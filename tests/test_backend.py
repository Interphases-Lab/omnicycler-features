import asyncio
import json

import pytest
from aurora_unicycler import CyclingProtocol

from omnicycler.features import backend as backend_module
from omnicycler.features.backend import ENDED_STATES, UnknownChannelError
from omnicycler.features.errors import (
    ChannelBusyError,
    ChannelNotClearedError,
    ChannelUnavailableError,
    DataRangeTooLargeError,
    NoActiveProtocolError,
    NoRunError,
    RunIdInUseError,
    RunMismatchError,
    UnsupportedProtocolError,
)
from omnicycler.features.simulator import SimulatedCyclerBackend
from omnicycler.features.types import BdfFormat, ChannelState, Sample
from tests.helpers import make_protocol

SHORT = [{"step": "open_circuit_voltage", "until_time_s": 300}]
LONG = [{"step": "open_circuit_voltage", "until_time_s": 1e9}]
SAMPLE = Sample(name="test-cell", nominal_capacity=1.0, identifier="CELL-7")


@pytest.fixture
async def backend():
    async with SimulatedCyclerBackend(channel_count=3, time_scale=1e4, unavailable_channels=[3], tick=0.01) as cycler:
        yield cycler


async def start(cycler: SimulatedCyclerBackend, channel: int, run_id: str, protocol: CyclingProtocol) -> None:
    await cycler.start_protocol(channel, run_id, protocol, SAMPLE)


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


async def test_unavailable_channel_cannot_start(backend):
    with pytest.raises(ChannelUnavailableError):
        await start(backend, 3, "run-a", make_protocol(SHORT))
    await backend.clear_channel(3, "run-a")  # holds no run: no effect
    with pytest.raises(NoRunError):
        await backend.get_run_information(3)


async def test_status_names_the_run_from_start_to_clear(backend):
    assert (await backend.get_channel_status(1)).run_id == ""
    await start(backend, 1, "run-a", make_protocol(SHORT))
    assert (await backend.get_channel_status(1)).run_id == "run-a"
    assert (await backend.get_run_information(1)).run_id == "run-a"
    assert (await backend.get_run_information(1)).sample == SAMPLE

    await wait_for(backend, 1, ENDED_STATES)
    assert (await backend.get_channel_status(1)).run_id == "run-a"
    await backend.clear_channel(1, "run-a")
    assert (await backend.get_channel_status(1)).run_id == ""


async def test_clearing_deletes_the_run(backend):
    await start(backend, 1, "run-a", make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)
    assert await backend.get_data(1, "run-a", 0, 10)

    with pytest.raises(ChannelNotClearedError):
        await start(backend, 1, "run-b", make_protocol(SHORT))
    await backend.clear_channel(1, "run-a")
    await backend.clear_channel(1, "run-a")

    status = await backend.get_channel_status(1)
    assert status.state is ChannelState.IDLE
    assert status.record_count == 0
    with pytest.raises(NoRunError):
        await backend.get_data(1, "run-a", 0, 10)
    with pytest.raises(NoRunError):
        await backend.get_run_information(1)


async def test_start_is_idempotent(backend):
    await start(backend, 1, "run-a", make_protocol(LONG))
    first = await backend.get_run_information(1)

    await start(backend, 1, "run-a", make_protocol(LONG))

    assert await backend.get_run_information(1) == first


async def test_run_id_held_by_another_channel_is_refused(backend):
    await start(backend, 1, "run-a", make_protocol(LONG))

    with pytest.raises(RunIdInUseError):
        await start(backend, 2, "run-a", make_protocol(LONG))
    assert (await backend.get_channel_status(2)).state is ChannelState.IDLE


async def test_concurrent_starts_with_one_run_id_start_it_once(backend):
    outcomes = await asyncio.gather(
        start(backend, 1, "run-a", make_protocol(LONG)),
        start(backend, 2, "run-a", make_protocol(LONG)),
        return_exceptions=True,
    )

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, RunIdInUseError) for outcome in outcomes) == 1


async def test_another_run_is_never_acted_on_or_read(backend):
    await start(backend, 1, "run-a", make_protocol(LONG))

    for operation in (
        backend.stop_protocol(1, "run-b"),
        backend.pause_protocol(1, "run-b"),
        backend.resume_protocol(1, "run-b"),
        backend.clear_channel(1, "run-b"),
        backend.get_record_count(1, "run-b"),
        backend.get_data(1, "run-b", 0, 10),
        backend.get_data_table(1, "run-b", 0, 10, []),
        backend.get_data_in_time_range(1, "run-b", 0, 10),
        backend.export_data(1, "run-b", BdfFormat.CSV),
    ):
        with pytest.raises(RunMismatchError):
            await operation
    with pytest.raises(RunMismatchError):
        _ = [batch async for batch in backend.monitor(1, "run-b", 0, maximal_duration=1)]

    assert (await backend.get_channel_status(1)).state is ChannelState.RUNNING


async def test_state_machine_of_an_active_run(backend):
    with pytest.raises(NoActiveProtocolError):
        await backend.resume_protocol(1, "run-a")

    await start(backend, 1, "run-a", make_protocol(LONG))
    with pytest.raises(ChannelBusyError):
        await start(backend, 1, "run-b", make_protocol(LONG))
    with pytest.raises(ChannelBusyError):
        await backend.clear_channel(1, "run-a")

    await backend.pause_protocol(1, "run-a")
    assert (await backend.get_channel_status(1)).state is ChannelState.PAUSED
    await backend.resume_protocol(1, "run-a")
    assert (await backend.get_channel_status(1)).state is ChannelState.RUNNING
    await backend.stop_protocol(1, "run-a")
    assert (await backend.get_channel_status(1)).state is ChannelState.STOPPED
    with pytest.raises(NoActiveProtocolError):
        await backend.pause_protocol(1, "run-a")


async def test_monitor_delivers_every_record_and_reports_the_end(backend):
    await start(backend, 1, "run-a", make_protocol(SHORT))

    monitor = backend.monitor(1, "run-a", 0, maximal_duration=5, poll_interval=0.01)
    batches = [batch async for batch in monitor]

    indices = [point.record_index for batch in batches for point in batch]
    assert monitor.run_ended
    assert indices == list(range(len(indices)))
    assert monitor.next_index == (await backend.get_channel_status(1)).record_count


async def test_monitor_stops_after_its_maximal_duration(backend):
    await start(backend, 1, "run-a", make_protocol(LONG))

    monitor = backend.monitor(1, "run-a", 0, maximal_duration=0.05, poll_interval=0.01)
    async with asyncio.timeout(2):
        _ = [batch async for batch in monitor]

    assert not monitor.run_ended
    assert monitor.next_index > 0


async def test_monitor_fails_when_its_run_is_deleted(backend):
    await start(backend, 1, "run-a", make_protocol(LONG))
    monitor = backend.monitor(1, "run-a", 0, maximal_duration=5, poll_interval=0.01)

    async def replace_the_run() -> None:
        await asyncio.sleep(0.05)
        await backend.stop_protocol(1, "run-a")
        await backend.clear_channel(1, "run-a")
        await start(backend, 1, "run-b", make_protocol(LONG))

    replacing = asyncio.create_task(replace_the_run())
    with pytest.raises((NoRunError, RunMismatchError)):
        async with asyncio.timeout(2):
            _ = [batch async for batch in monitor]
    await replacing


async def test_time_range_is_inclusive(backend):
    await start(backend, 1, "run-a", make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    points = await backend.get_data_in_time_range(1, "run-a", 100.0, 200.0)

    assert [point.test_time for point in points] == pytest.approx([100.0 + 10 * index for index in range(11)])


async def test_too_large_time_range_is_refused(backend, monkeypatch):
    monkeypatch.setattr(backend_module, "MAXIMAL_RECORDS_PER_RESPONSE", 5)
    await start(backend, 1, "run-a", make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    with pytest.raises(DataRangeTooLargeError):
        await backend.get_data_in_time_range(1, "run-a", 0.0, 300.0)


async def test_export_without_run_is_refused(backend):
    with pytest.raises(NoRunError):
        await backend.export_data(1, "run-a", BdfFormat.CSV)


async def test_export_cut_short_by_a_clear_is_refused(backend, monkeypatch):
    monkeypatch.setattr(backend_module, "MAXIMAL_RECORDS_PER_RESPONSE", 5)
    await start(backend, 1, "run-a", make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)
    read_data = backend.read_data
    pages = 0

    async def read_and_clear(channel: int, run_id: str, from_index: int, count: int) -> list:
        nonlocal pages
        pages += 1
        if pages == 2:
            await backend.clear(channel)  # a client clears the channel during the export
        return await read_data(channel, run_id, from_index, count)

    monkeypatch.setattr(backend, "read_data", read_and_clear)
    with pytest.raises(NoRunError):
        await backend.export_data(1, "run-a", BdfFormat.CSV)


async def test_fault_reason_is_reported_until_the_channel_is_cleared(backend):
    method = [{"step": "constant_current", "rate_C": 1, "until_time_s": 3600}]
    await start(backend, 1, "run-a", make_protocol(method, safety={"max_voltage_V": 3.8}))
    await wait_for(backend, 1, ENDED_STATES)

    status = await backend.get_channel_status(1)
    assert status.state is ChannelState.FAULTED
    assert "maximal voltage" in status.fault_reason
    assert (await backend.get_channel_status(2)).fault_reason == ""

    await backend.clear_channel(1, "run-a")
    assert (await backend.get_channel_status(1)).fault_reason == ""


async def test_export_carries_the_run_metadata(backend):
    await start(backend, 1, "run-a", make_protocol(SHORT))
    await wait_for(backend, 1, ENDED_STATES)

    export = await backend.export_data(1, "run-a", BdfFormat.CSV)

    sidecar = json.loads(export.metadata)
    test = sidecar["battinfo_test"]["test"]
    assert test["identifier"] == "run-a"
    assert test["status"] == "completed"
    assert sidecar["battinfo_cell"]["cell_instance"] == {"name": "test-cell", "serial_number": "CELL-7"}
    assert sidecar["battinfo_equipment"]["equipment"]["serial_number"] == "SIM-0001"
    assert sidecar["battinfo_channel"]["channel"]["index"] == 1
    assert sidecar["extras"]["aurora_unicycler_protocol"]["sample"]["name"] == "test-cell"
