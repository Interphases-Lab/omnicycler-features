import asyncio
import json

import pytest

from omnicycler.features import backend as backend_module
from omnicycler.features.backend import ENDED_STATES, UnknownChannelError
from omnicycler.features.errors import (
    ChannelBusyError,
    ChannelNotClearedError,
    ChannelUnavailableError,
    DataRangeTooLargeError,
    NoActiveProtocolError,
    NoRunError,
    UnsupportedProtocolError,
)
from omnicycler.features.simulator import SimulatedCyclerBackend
from omnicycler.features.types import BdfFormat, ChannelState
from tests.helpers import make_protocol

SHORT = [{"step": "open_circuit_voltage", "until_time_s": 300}]
LONG = [{"step": "open_circuit_voltage", "until_time_s": 1e9}]


@pytest.fixture
async def backend():
    async with SimulatedCyclerBackend(channel_count=3, time_scale=1e4, unavailable_channels=[3], tick=0.01) as cycler:
        yield cycler


async def wait_for(cycler: SimulatedCyclerBackend, channel: int, states: frozenset[ChannelState]) -> None:
    async with asyncio.timeout(5):
        while (await cycler.get_channel_status(channel)).state not in states:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("method", "message"),
    [
        (
            [{"step": "impedance_spectroscopy", "amplitude_V": 0.01, "start_frequency_Hz": 1e3, "end_frequency_Hz": 1}],
            "cannot execute",
        ),
        ([{"step": "constant_current", "current_mA": 3000, "until_time_s": 1}], "needs 3 A"),
        ([{"step": "constant_current", "rate_C": 1, "until_voltage_V": 6}], "outside channel"),
        ([{"step": "constant_voltage", "voltage_V": 5.5, "until_time_s": 1}], "outside channel"),
        (
            [{"step": "voltage_scan", "start_voltage_V": 3, "end_voltage_V": 7, "scan_rate_mV_per_s": 1}],
            "outside channel",
        ),
    ],
)
async def test_protocols_beyond_the_channel_are_rejected(backend, method, message):
    with pytest.raises(UnsupportedProtocolError, match=message):
        await backend.check_protocol(1, make_protocol(method))


async def test_supported_protocol_passes_the_checks(backend):
    method = [{"step": "constant_current", "rate_C": 1, "until_voltage_V": 4.2}]

    assert await backend.check_protocol(1, make_protocol(method)) == []


async def test_unknown_channel_is_refused(backend):
    with pytest.raises(UnknownChannelError):
        await backend.get_channel_status(9)


async def test_unavailable_channel_cannot_start_or_clear(backend):
    with pytest.raises(ChannelUnavailableError):
        await backend.start_protocol(3, make_protocol(SHORT))
    with pytest.raises(ChannelUnavailableError):
        await backend.clear_channel(3)
    with pytest.raises(NoRunError):
        await backend.get_run_information(3)


async def test_clearing_releases_the_run(backend):
    await backend.start_protocol(1, make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)
    assert await backend.get_data(1, 0, 10)

    with pytest.raises(ChannelNotClearedError):
        await backend.start_protocol(1, make_protocol(SHORT))
    await backend.clear_channel(1)

    status = await backend.get_channel_status(1)
    assert status.state is ChannelState.IDLE
    assert status.record_count == 0
    assert await backend.get_data(1, 0, 10) == []
    with pytest.raises(NoRunError):
        await backend.get_run_information(1)


async def test_state_machine_of_an_active_run(backend):
    with pytest.raises(NoActiveProtocolError):
        await backend.resume_protocol(1)

    await backend.start_protocol(1, make_protocol(LONG))
    with pytest.raises(ChannelBusyError):
        await backend.start_protocol(1, make_protocol(LONG))
    with pytest.raises(ChannelBusyError):
        await backend.clear_channel(1)

    await backend.pause_protocol(1)
    assert (await backend.get_channel_status(1)).state is ChannelState.PAUSED
    await backend.resume_protocol(1)
    assert (await backend.get_channel_status(1)).state is ChannelState.RUNNING
    await backend.stop_protocol(1)
    assert (await backend.get_channel_status(1)).state is ChannelState.STOPPED
    with pytest.raises(NoActiveProtocolError):
        await backend.pause_protocol(1)


async def test_monitor_delivers_every_record_and_reports_the_end(backend):
    await backend.start_protocol(1, make_protocol(SHORT))

    monitor = backend.monitor(1, 0, maximal_duration=5, poll_interval=0.01)
    batches = [batch async for batch in monitor]

    indices = [point.record_index for batch in batches for point in batch]
    assert monitor.run_ended
    assert indices == list(range(len(indices)))
    assert monitor.next_index == (await backend.get_channel_status(1)).record_count


async def test_monitor_stops_after_its_maximal_duration(backend):
    await backend.start_protocol(1, make_protocol(LONG))

    monitor = backend.monitor(1, 0, maximal_duration=0.05, poll_interval=0.01)
    async with asyncio.timeout(2):
        _ = [batch async for batch in monitor]

    assert not monitor.run_ended
    assert monitor.next_index > 0


async def test_time_range_is_inclusive(backend):
    await backend.start_protocol(1, make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    points = await backend.get_data_in_time_range(1, 100.0, 200.0)

    assert [point.test_time for point in points] == pytest.approx([100.0 + 10 * index for index in range(11)])


async def test_too_large_time_range_is_refused(backend, monkeypatch):
    monkeypatch.setattr(backend_module, "MAXIMAL_RECORDS_PER_RESPONSE", 5)
    await backend.start_protocol(1, make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    with pytest.raises(DataRangeTooLargeError):
        await backend.get_data_in_time_range(1, 0.0, 300.0)


async def test_export_without_run_is_refused(backend):
    with pytest.raises(NoRunError):
        await backend.export_data(1, BdfFormat.CSV)


async def test_every_run_gets_its_own_identifier(backend):
    await backend.start_protocol(1, make_protocol(SHORT))
    await backend.start_protocol(2, make_protocol(SHORT))
    first = (await backend.get_run_information(1)).run_id
    assert first != (await backend.get_run_information(2)).run_id

    await wait_for(backend, 1, ENDED_STATES)
    assert (await backend.get_run_information(1)).run_id == first
    await backend.clear_channel(1)
    await backend.start_protocol(1, make_protocol(SHORT))

    assert (await backend.get_run_information(1)).run_id not in {first, ""}


async def test_fault_reason_is_reported_until_the_channel_is_cleared(backend):
    method = [{"step": "constant_current", "rate_C": 1, "until_time_s": 3600}]
    await backend.start_protocol(1, make_protocol(method, safety={"max_voltage_V": 3.8}))
    await wait_for(backend, 1, ENDED_STATES)

    status = await backend.get_channel_status(1)
    assert status.state is ChannelState.FAULTED
    assert "maximal voltage" in status.fault_reason
    assert (await backend.get_channel_status(2)).fault_reason == ""

    await backend.clear_channel(1)
    assert (await backend.get_channel_status(1)).fault_reason == ""


async def test_export_carries_the_run_metadata(backend):
    await backend.start_protocol(1, make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    export = await backend.export_data(1, BdfFormat.CSV)

    sidecar = json.loads(export.metadata)
    test = sidecar["battinfo_test"]["test"]
    assert test["identifier"] == (await backend.get_run_information(1)).run_id
    assert test["status"] == "completed"
    assert sidecar["battinfo_equipment"]["equipment"]["serial_number"] == "SIM-0001"
    assert sidecar["battinfo_channel"]["channel"]["index"] == 1
    assert sidecar["extras"]["aurora_unicycler_protocol"]["sample"]["name"] == "test-cell"
