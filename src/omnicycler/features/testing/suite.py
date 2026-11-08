"""The OmniCycler conformance suite: what every brand server must pass in simulation mode."""

import asyncio
import collections.abc
import io
import itertools
import json
import math
import pathlib

import pytest

from ..backend import ENDED_STATES
from ..client import InvalidParameterError, OmniCyclerClient
from ..data_table import COLUMNS, INTEGER_FIELDS, table_column
from ..errors import ChannelBusyError, ChannelNotClearedError, NoActiveProtocolError, NoRunError
from ..types import (
    BdfFormat,
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    MeasuredQuantity,
    ProtocolFormat,
    Sample,
    StepType,
)
from .protocols import sample_for

OPTIONAL_FIELDS = {quantity: quantity.name.lower() for quantity in MeasuredQuantity}
"""The `DataPoint` field of each optional measured quantity."""

STEP_EXAMPLES: dict[StepType, dict[str, object]] = {
    StepType.OPEN_CIRCUIT_VOLTAGE: {"step": "open_circuit_voltage", "until_time_s": 10},
    StepType.CONSTANT_CURRENT: {"step": "constant_current", "rate_C": 0.1, "until_time_s": 10},
    StepType.CONSTANT_VOLTAGE: {"step": "constant_voltage", "voltage_V": 3.7, "until_time_s": 10},
    StepType.IMPEDANCE_SPECTROSCOPY: {
        "step": "impedance_spectroscopy",
        "amplitude_V": 0.01,
        "start_frequency_Hz": 1e4,
        "end_frequency_Hz": 1,
    },
    StepType.VOLTAGE_SCAN: {
        "step": "voltage_scan",
        "start_voltage_V": 3.6,
        "end_voltage_V": 3.8,
        "scan_rate_mV_per_s": 1,
    },
}


class ConformanceSuite:
    """
    Tests every OmniCycler server must pass in simulation mode.

    Use it in a brand repo by subclassing it in a test module, with the
    fixtures of `omnicycler.features.testing.plugin` enabled:

        class TestConformance(ConformanceSuite):
            pass
    """

    pytestmark = pytest.mark.asyncio

    # Identity and channels.

    async def test_identity_is_reported(self, omnicycler_client: OmniCyclerClient) -> None:
        information = omnicycler_client.information
        values = [
            await information.get_manufacturer(),
            await information.get_model(),
            await information.get_serial_number(),
            await information.get_firmware_version(),
            await information.get_unicycler_version(),
        ]
        assert all(value.strip() for value in values)

    async def test_channels_are_consistent(self, omnicycler_client: OmniCyclerClient) -> None:
        channels = await omnicycler_client.information.get_channels()
        numbers = [channel.channel for channel in channels]
        assert numbers
        assert len(set(numbers)) == len(numbers)
        for channel in channels:
            assert channel.minimal_voltage < channel.maximal_voltage
            assert channel.maximal_current > 0
            assert len(set(channel.measured_quantities)) == len(channel.measured_quantities)

    async def test_supported_step_types_are_listed(self, omnicycler_client: OmniCyclerClient) -> None:
        step_types = await omnicycler_client.information.get_supported_step_types()
        assert step_types
        assert len(set(step_types)) == len(step_types)

    async def test_status_stream_reports_every_channel(self, omnicycler_client: OmniCyclerClient) -> None:
        numbers = {channel.channel for channel in await omnicycler_client.information.get_channels()}
        async with asyncio.timeout(10), omnicycler_client.channels.subscribe_channel_statuses() as statuses:
            snapshot = await anext(statuses)
        assert sorted(status.channel for status in snapshot) == sorted(numbers)

    # Validation.

    async def test_unknown_channel_is_rejected(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channels = await omnicycler_client.information.get_channels()
        unknown = max(channel.channel for channel in channels) + 1
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(unknown, omnicycler_short_protocol, sample_for(channels[0]))
        assert error.value.parameter == "Channel"

    async def test_invalid_protocol_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(channel.channel, b"not json", sample_for(channel))
        assert error.value.parameter == "Protocol"
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    async def test_placeholder_sample_name_is_rejected(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = Sample(name="$NAME", nominal_capacity=sample_for(channel).nominal_capacity)
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_short_protocol, sample)
        assert error.value.parameter == "Sample"

    async def test_unsupported_step_type_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        supported = set(await omnicycler_client.information.get_supported_step_types())
        unsupported = [step_type for step_type in StepType if step_type not in supported]
        if not unsupported:
            pytest.skip("The server supports every step type.")
        channel = await self._idle_channel(omnicycler_client)
        protocol = json.dumps({"record": {"time_s": 10}, "method": [STEP_EXAMPLES[unsupported[0]]]}).encode()
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.validate_protocol(channel.channel, protocol, sample_for(channel))
        assert error.value.parameter == "Protocol"

    async def test_current_above_channel_limit_is_rejected(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        step = {"step": "constant_current", "current_mA": channel.maximal_current * 2000, "until_time_s": 10}
        protocol = json.dumps({"record": {"time_s": 10}, "method": [step]}).encode()
        with pytest.raises(InvalidParameterError) as error:
            await omnicycler_client.channels.validate_protocol(channel.channel, protocol, sample_for(channel))
        assert error.value.parameter == "Protocol"

    async def test_validate_protocol_does_not_start(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        warnings = await omnicycler_client.channels.validate_protocol(
            channel.channel, omnicycler_short_protocol, sample_for(channel)
        )
        assert all(isinstance(warning, str) for warning in warnings)
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    # Channel lifecycle.

    async def test_idle_channel_holds_no_run(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(NoRunError):
            await omnicycler_client.channels.get_run_information(channel.channel)
        assert await omnicycler_client.measurements.get_record_count(channel.channel) == 0
        assert await omnicycler_client.measurements.get_data(channel.channel, 0, 10) == []

    async def test_protocol_runs_to_completion(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = sample_for(channel)
        await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_short_protocol, sample)

        status = await omnicycler_client.channels.wait_for_state(
            channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout
        )
        assert status.state is ChannelState.FINISHED

        assert status.fault_reason == ""
        run = await omnicycler_client.channels.get_run_information(channel.channel)
        assert run.run_id
        assert run.sample.name == sample.name
        assert math.isclose(run.sample.nominal_capacity, sample.nominal_capacity, rel_tol=1e-9)
        assert run.protocol_format is ProtocolFormat.AURORA_UNICYCLER
        assert json.loads(run.protocol)["sample"]["name"] == sample.name

        points = await omnicycler_client.measurements.get_all_data(channel.channel)
        assert points
        assert [point.record_index for point in points] == list(range(len(points)))
        assert len(points) == await omnicycler_client.measurements.get_record_count(channel.channel)
        assert all(later.test_time >= earlier.test_time for earlier, later in itertools.pairwise(points))

    async def test_ended_run_must_be_cleared(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        await channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))
        await channels.wait_for_state(channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout)
        first_run = (await channels.get_run_information(channel.channel)).run_id

        with pytest.raises(ChannelNotClearedError):
            await channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))

        await channels.clear_channel(channel.channel)
        await channels.clear_channel(channel.channel)
        assert (await channels.get_channel_status(channel.channel)).state is ChannelState.IDLE
        assert await omnicycler_client.measurements.get_record_count(channel.channel) == 0
        await channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))
        assert (await channels.get_run_information(channel.channel)).run_id != first_run

    async def test_busy_channel_rejects_start_and_clear(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))

        with pytest.raises(ChannelBusyError):
            await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        with pytest.raises(ChannelBusyError):
            await channels.clear_channel(channel.channel)

    async def test_stop_is_idempotent(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        await channels.stop_protocol(channel.channel)

        await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        await channels.stop_protocol(channel.channel)
        status = await channels.wait_for_state(channel.channel, ENDED_STATES, timeout=10)
        assert status.state is ChannelState.STOPPED
        await channels.stop_protocol(channel.channel)

        await omnicycler_client.channels.get_run_information(channel.channel)
        await channels.clear_channel(channel.channel)
        assert (await channels.get_channel_status(channel.channel)).state is ChannelState.IDLE

    async def test_pause_and_resume(self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes) -> None:
        channels = omnicycler_client.channels
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(NoActiveProtocolError):
            await channels.pause_protocol(channel.channel)

        await channels.start_protocol(channel.channel, omnicycler_long_protocol, sample_for(channel))
        await channels.pause_protocol(channel.channel)
        await channels.pause_protocol(channel.channel)
        await channels.wait_for_state(channel.channel, {ChannelState.PAUSED}, timeout=10)
        await channels.resume_protocol(channel.channel)
        await channels.resume_protocol(channel.channel)
        await channels.wait_for_state(channel.channel, {ChannelState.RUNNING}, timeout=10)

    async def test_safety_limit_faults_with_a_reason(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_faulting_protocol: collections.abc.Callable[[Sample], bytes],
        omnicycler_run_timeout: float,
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        sample = sample_for(channel)
        await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_faulting_protocol(sample), sample)

        status = await omnicycler_client.channels.wait_for_state(
            channel.channel, ENDED_STATES, timeout=omnicycler_run_timeout
        )

        assert status.state is ChannelState.FAULTED
        assert status.fault_reason
        for other in await self._statuses(omnicycler_client):
            assert other.state is ChannelState.FAULTED or other.fault_reason == ""
        await omnicycler_client.channels.clear_channel(channel.channel)
        assert (await omnicycler_client.channels.get_channel_status(channel.channel)).fault_reason == ""

    async def test_concurrent_starts_on_different_channels(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        idle = await self._idle_channels(omnicycler_client)
        if len(idle) < 2:
            pytest.skip("Needs two idle channels.")
        await asyncio.gather(
            *(
                omnicycler_client.channels.start_protocol(
                    channel.channel, omnicycler_long_protocol, sample_for(channel)
                )
                for channel in idle[:2]
            )
        )
        for channel in idle[:2]:
            await omnicycler_client.channels.wait_for_state(channel.channel, {ChannelState.RUNNING}, timeout=10)

    async def test_concurrent_starts_on_one_channel(
        self, omnicycler_client: OmniCyclerClient, omnicycler_long_protocol: bytes
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        outcomes = await asyncio.gather(
            *(
                omnicycler_client.channels.start_protocol(
                    channel.channel, omnicycler_long_protocol, sample_for(channel)
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, ChannelBusyError) for outcome in outcomes) == 1

    # Data.

    async def test_time_range_matches_paging(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._finished_channel(omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout)
        points = await omnicycler_client.measurements.get_all_data(channel.channel)
        start, end = points[len(points) // 4].test_time, points[3 * len(points) // 4].test_time

        ranged = await omnicycler_client.measurements.get_data_in_time_range(channel.channel, start, end)
        expected = [point.record_index for point in points if start <= point.test_time <= end]
        assert [point.record_index for point in ranged] == expected

    async def test_data_table_matches_get_data(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._finished_channel(omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout)
        points = await omnicycler_client.measurements.get_all_data(channel.channel)
        table = await omnicycler_client.measurements.get_data_table(channel.channel, 0, len(points) + 10)

        assert table.num_rows == len(points)
        for field, label in COLUMNS.items():
            expected = [float(getattr(point, field)) for point in points]
            if all(math.isnan(value) for value in expected):
                assert label not in table.column_names, label
                continue
            assert table_column(table, field) == pytest.approx(expected, nan_ok=True), label
            if field in INTEGER_FIELDS:
                assert str(table.schema.field(label).type) == "int64", label

        middle = len(points) // 2
        part = await omnicycler_client.measurements.get_data_table(
            channel.channel, middle, 3, [COLUMNS["record_index"], COLUMNS["voltage"]]
        )
        assert part.column_names == [COLUMNS["record_index"], COLUMNS["voltage"]]
        assert part.column(COLUMNS["record_index"]).to_pylist() == [middle, middle + 1, middle + 2]

    async def test_monitor_streams_every_record_once(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._idle_channel(omnicycler_client)
        await omnicycler_client.channels.start_protocol(channel.channel, omnicycler_short_protocol, sample_for(channel))

        async with (
            asyncio.timeout(omnicycler_run_timeout),
            omnicycler_client.measurements.monitor(channel.channel, maximal_duration=5) as records,
        ):
            streamed = [point.record_index async for point in records]

        assert streamed == list(range(await omnicycler_client.measurements.get_record_count(channel.channel)))

    async def test_unmeasured_quantities_are_nan(
        self, omnicycler_client: OmniCyclerClient, omnicycler_short_protocol: bytes, omnicycler_run_timeout: float
    ) -> None:
        channel = await self._finished_channel(omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout)
        unmeasured = [
            field for quantity, field in OPTIONAL_FIELDS.items() if quantity not in channel.measured_quantities
        ]
        for point in await omnicycler_client.measurements.get_all_data(channel.channel):
            assert all(math.isnan(getattr(point, field)) for field in unmeasured)

    @pytest.mark.parametrize("data_format", list(BdfFormat))
    async def test_export_is_valid_bdf(
        self,
        omnicycler_client: OmniCyclerClient,
        omnicycler_short_protocol: bytes,
        omnicycler_run_timeout: float,
        data_format: BdfFormat,
        tmp_path: pathlib.Path,
    ) -> None:
        polars = pytest.importorskip("polars")
        bdf = pytest.importorskip("bdf")
        channel = await self._finished_channel(omnicycler_client, omnicycler_short_protocol, omnicycler_run_timeout)

        export = await omnicycler_client.measurements.export_data(channel.channel, data_format)

        reader = polars.read_csv if data_format is BdfFormat.CSV else polars.read_parquet
        frame = reader(io.BytesIO(export.data))
        report = bdf.validate_df(frame, raise_on_error=True)
        assert report["extras"] == []
        assert report["derived"]["issues"] == []
        assert len(frame) == await omnicycler_client.measurements.get_record_count(channel.channel)
        floats = [name for name, dtype in frame.schema.items() if dtype.is_float()]
        assert not any(frame[name].is_nan().all() for name in floats)

        table, _ = export.save(tmp_path / f"run.bdf.{data_format.name.lower()}")
        _, metadata = bdf.read(table)
        test = metadata.battinfo_test.test
        assert test.identifier == (await omnicycler_client.channels.get_run_information(channel.channel)).run_id
        assert test.status == "completed"
        assert metadata.battinfo_channel.channel.index == channel.channel
        assert metadata.battinfo_equipment.equipment.serial_number
        assert metadata.battinfo_test_protocol.method

    async def test_export_without_run_raises(self, omnicycler_client: OmniCyclerClient) -> None:
        channel = await self._idle_channel(omnicycler_client)
        with pytest.raises(NoRunError):
            await omnicycler_client.measurements.export_data(channel.channel, BdfFormat.CSV)

    # Helpers.

    async def _idle_channels(self, client: OmniCyclerClient) -> list[ChannelInformation]:
        statuses = {status.channel: status.state for status in await self._statuses(client)}
        return [
            channel
            for channel in await client.information.get_channels()
            if statuses.get(channel.channel) is ChannelState.IDLE
        ]

    async def _idle_channel(self, client: OmniCyclerClient) -> ChannelInformation:
        idle = await self._idle_channels(client)
        if not idle:
            pytest.skip("Needs an idle channel.")
        return idle[0]

    async def _finished_channel(self, client: OmniCyclerClient, protocol: bytes, timeout: float) -> ChannelInformation:
        channel = await self._idle_channel(client)
        await client.channels.start_protocol(channel.channel, protocol, sample_for(channel))
        status = await client.channels.wait_for_state(channel.channel, ENDED_STATES, timeout=timeout)
        assert status.state is ChannelState.FINISHED
        return channel

    @staticmethod
    async def _statuses(client: OmniCyclerClient) -> list[ChannelStatus]:
        async with asyncio.timeout(10), client.channels.subscribe_channel_statuses() as statuses:
            return await anext(statuses)
