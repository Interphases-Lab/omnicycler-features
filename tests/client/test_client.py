import dataclasses
import json
import math
import socket
import typing

import pytest
from aurora_unicycler import CyclingProtocol
from sila.client import ClientConfig
from unitelabs.cdk import Connector, ConnectorBaseConfig

from omnicycler.features.client import (
    IncompatibleServerError,
    InvalidParameterError,
    MeasurementProviderClient,
    MonitorResult,
    OmniCyclerClient,
    ProtocolMismatchError,
)
from omnicycler.features.client._base import from_wire, list_from_wire, to_wire
from omnicycler.features.cycler_information_provider import CyclerInformationProvider
from omnicycler.features.errors import ChannelBusyError
from omnicycler.features.simulator import SimulatedCyclerBackend
from omnicycler.features.testing import long_protocol, sample_for, short_protocol
from omnicycler.features.types import (
    BdfExport,
    BdfFormat,
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    DataPoint,
    RunInformation,
    Sample,
    StepType,
)
from tests.helpers import make_protocol

ENDED = {ChannelState.FINISHED, ChannelState.STOPPED, ChannelState.FAULTED}


@pytest.fixture
async def cycler(omnicycler_client: OmniCyclerClient) -> OmniCyclerClient:
    return omnicycler_client


@pytest.fixture
async def channel(cycler: OmniCyclerClient) -> ChannelInformation:
    return (await cycler.information.get_channels())[0]


async def test_public_api_is_fully_typed(cycler: OmniCyclerClient, channel: ChannelInformation):
    number, sample = channel.channel, sample_for(channel)

    typing.assert_type(await cycler.information.get_model(), str)
    typing.assert_type(await cycler.information.get_channels(), list[ChannelInformation])
    typing.assert_type(await cycler.information.get_supported_step_types(), list[StepType])
    typing.assert_type(await cycler.channels.validate_protocol(number, short_protocol(), sample), list[str])
    typing.assert_type(await cycler.channels.start_protocol(number, short_protocol(), sample), None)
    status = await cycler.channels.wait_for_state(number, ENDED, timeout=30)
    typing.assert_type(status, ChannelStatus)
    typing.assert_type(await cycler.channels.get_channel_status(number), ChannelStatus)
    typing.assert_type(await cycler.channels.get_run_information(number), RunInformation)
    typing.assert_type(await cycler.measurements.get_record_count(number), int)
    typing.assert_type(await cycler.measurements.get_data(number, 0, 10), list[DataPoint])
    typing.assert_type(await cycler.measurements.get_data_in_time_range(number, 0, 10), list[DataPoint])
    typing.assert_type(await cycler.measurements.monitor_channel(number, 0, 1), MonitorResult)
    typing.assert_type(await cycler.measurements.export_data(number, BdfFormat.CSV), BdfExport)
    async with cycler.channels.subscribe_channel_statuses() as statuses:
        typing.assert_type(await anext(statuses), list[ChannelStatus])
    async with cycler.measurements.monitor(number) as records:
        typing.assert_type(await anext(records), DataPoint)


async def test_aurora_unicycler_protocol_objects_are_accepted(cycler: OmniCyclerClient, channel: ChannelInformation):
    protocol: CyclingProtocol = make_protocol([{"step": "open_circuit_voltage", "until_time_s": 60}])

    await cycler.channels.start_protocol(channel.channel, protocol, sample_for(channel))

    assert (await cycler.channels.get_run_information(channel.channel)).sample.name == sample_for(channel).name


async def test_server_errors_arrive_as_shared_classes(cycler: OmniCyclerClient, channel: ChannelInformation):
    await cycler.channels.start_protocol(channel.channel, long_protocol(), sample_for(channel))

    with pytest.raises(ChannelBusyError, match="stop its protocol first"):
        await cycler.channels.start_protocol(channel.channel, long_protocol(), sample_for(channel))
    with pytest.raises(InvalidParameterError) as error:
        await cycler.channels.validate_protocol(channel.channel, b"{}", sample_for(channel))
    assert error.value.parameter == "Protocol"
    with pytest.raises(InvalidParameterError) as error:
        await cycler.measurements.get_data_in_time_range(channel.channel, 10, 5)
    assert error.value.parameter == "ToTestTime"


async def test_unmeasured_values_arrive_as_nan(cycler: OmniCyclerClient, channel: ChannelInformation):
    await cycler.channels.start_protocol(channel.channel, short_protocol(), sample_for(channel))
    await cycler.channels.wait_for_state(channel.channel, ENDED, timeout=30)

    point = (await cycler.measurements.get_data(channel.channel, 0, 1))[0]

    assert math.isnan(point.temperature_t2)
    assert point.ambient_temperature == 25.0


async def test_run_id_and_fault_reason_cross_the_wire(cycler: OmniCyclerClient, channel: ChannelInformation):
    method = [{"step": "constant_current", "rate_C": 1, "until_time_s": 3600}]
    protocol = make_protocol(method, capacity=sample_for(channel).nominal_capacity, safety={"max_voltage_V": 3.8})
    await cycler.channels.start_protocol(channel.channel, protocol, sample_for(channel))

    status = await cycler.channels.wait_for_state(channel.channel, ENDED, timeout=30)

    assert status.state is ChannelState.FAULTED
    assert "maximal voltage" in status.fault_reason
    run_id = (await cycler.channels.get_run_information(channel.channel)).run_id
    export = await cycler.measurements.export_data(channel.channel, BdfFormat.PARQUET)
    test = json.loads(export.metadata)["battinfo_test"]["test"]
    assert test["identifier"] == run_id
    assert test["conformance"]["deviations"][0]["description"] == status.fault_reason


async def test_server_without_standard_features_is_refused():
    port = _free_port()
    config = ConnectorBaseConfig.validate(
        {"sila_server": {"hostname": "127.0.0.1", "port": port, "tls": False}, "discovery": None}
    )
    app = Connector(config)
    app.register(CyclerInformationProvider(SimulatedCyclerBackend()))
    await app.start()
    try:
        with pytest.raises(IncompatibleServerError, match="ChannelController"):
            async with OmniCyclerClient.connect(ClientConfig(hostname="127.0.0.1", port=port, tls=False)):
                pass
    finally:
        await app.stop()


async def test_monitor_fills_gaps_from_get_data():
    stored = [_point(index) for index in range(10)]

    class Recorder(MeasurementProviderClient):
        async def get_data(self, channel, from_index, count, *, timeout=None):  # noqa: ARG002
            return stored[from_index : from_index + count]

    client = Recorder(typing.cast("typing.Any", None))

    assert [p.record_index for p in await client._fill(1, 2, stored[5:7])] == [2, 3, 4, 5, 6]
    assert [p.record_index for p in await client._fill(1, 6, stored[4:8])] == [6, 7]
    assert await client._fill(1, 9, stored[0:3]) == []


def test_to_wire_uses_identifiers_and_enum_strings():
    assert to_wire(Sample(name="cell", nominal_capacity=1.5)) == {"Name": "cell", "NominalCapacity": 1.5}
    assert to_wire([BdfFormat.CSV, BdfFormat.PARQUET]) == ["csv", "parquet"]


def test_from_wire_checks_the_shape():
    status = {
        "Channel": 1,
        "State": "running",
        "Voltage": 3.7,
        "Current": 0,
        "TestTime": 1,
        "CycleCount": 1,
        "ProtocolStep": 2,
        "RecordCount": 3,
        "FaultReason": "",
    }

    decoded = from_wire(ChannelStatus, status)

    assert decoded.state is ChannelState.RUNNING
    assert isinstance(decoded.current, float)
    for broken in ({**status, "State": "exploded"}, {**status, "Channel": "1"}, {"Channel": 1}, [status]):
        with pytest.raises(ProtocolMismatchError):
            from_wire(ChannelStatus, broken)
    with pytest.raises(ProtocolMismatchError):
        list_from_wire(StepType, "constant current")


def _point(index: int) -> DataPoint:
    explicit = {"record_index", "test_time", "cycle_count", "step_count", "protocol_step"}
    measured = {field.name for field in dataclasses.fields(DataPoint)} - explicit
    return DataPoint(
        record_index=index,
        test_time=float(index),
        cycle_count=1,
        step_count=1,
        protocol_step=1,
        **dict.fromkeys(measured, math.nan),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
