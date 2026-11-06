import asyncio
import contextlib
import dataclasses
import math
import time
import typing

from aurora_unicycler import CyclingProtocol

from .. import __version__
from ..backend import CyclerBackend, CyclerIdentity
from ..protocol import encode_protocol
from ..simulation import MEASURED_QUANTITIES, ProtocolSimulation
from ..types import (
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    DataPoint,
    ProtocolFormat,
    RunInformation,
    Sample,
    StepType,
)

SUPPORTED_STEP_TYPES = (
    StepType.OPEN_CIRCUIT_VOLTAGE,
    StepType.CONSTANT_CURRENT,
    StepType.CONSTANT_VOLTAGE,
    StepType.VOLTAGE_SCAN,
)
"""The simulator executes every technique except impedance spectroscopy."""


@dataclasses.dataclass
class _Run:
    run_id: str
    sample: Sample
    protocol: CyclingProtocol
    start_time: float
    simulation: ProtocolSimulation
    state: ChannelState = ChannelState.RUNNING
    points: list[DataPoint] = dataclasses.field(default_factory=list)


class SimulatedCyclerBackend(CyclerBackend):
    """
    A cycler simulated in software, running protocols on a simulated cell per channel.

    Simulated time runs `time_scale` times faster than wall-clock time while
    the backend is open.
    """

    def __init__(  # noqa: PLR0913  # keyword-only settings
        self,
        *,
        channel_count: int = 8,
        time_scale: float = 60.0,
        minimal_voltage: float = 0.0,
        maximal_voltage: float = 5.0,
        maximal_current: float = 2.0,
        unavailable_channels: typing.Collection[int] = (),
        tick: float = 0.05,
    ) -> None:
        super().__init__()
        self._channel_count = channel_count
        self._time_scale = time_scale
        self._minimal_voltage = minimal_voltage
        self._maximal_voltage = maximal_voltage
        self._maximal_current = maximal_current
        self._unavailable = frozenset(unavailable_channels)
        self._tick = tick
        self._runs: dict[int, _Run] = {}
        self._task: asyncio.Task[None] | None = None

    @typing.override
    async def open(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_clock())

    @typing.override
    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @typing.override
    async def read_identity(self) -> CyclerIdentity:
        return CyclerIdentity(
            manufacturer="OmniCycler",
            model="Simulated Cycler",
            serial_number="SIM-0001",
            firmware_version=__version__,
        )

    @typing.override
    async def read_channels(self) -> list[ChannelInformation]:
        return [
            ChannelInformation(
                channel=channel,
                minimal_voltage=self._minimal_voltage,
                maximal_voltage=self._maximal_voltage,
                maximal_current=self._maximal_current,
                measured_quantities=list(MEASURED_QUANTITIES),
            )
            for channel in range(1, self._channel_count + 1)
        ]

    @typing.override
    async def read_supported_step_types(self) -> list[StepType]:
        return list(SUPPORTED_STEP_TYPES)

    @typing.override
    async def read_statuses(self) -> list[ChannelStatus]:
        return [self._status(channel) for channel in range(1, self._channel_count + 1)]

    @typing.override
    async def read_run_information(self, channel: int) -> RunInformation:
        run = self._runs[channel]
        return RunInformation(
            run_id=run.run_id,
            sample=run.sample,
            protocol_format=ProtocolFormat.AURORA_UNICYCLER,
            protocol=encode_protocol(run.protocol),
            start_time=run.start_time,
        )

    @typing.override
    async def read_data(self, channel: int, from_index: int, count: int) -> list[DataPoint]:
        run = self._runs.get(channel)
        return [] if run is None else run.points[from_index : from_index + count]

    @typing.override
    async def start(self, channel: int, protocol: CyclingProtocol, run_id: str) -> None:
        start_time = time.time()
        sample = Sample(name=protocol.sample.name, nominal_capacity=(protocol.sample.capacity_mAh or 0.0) / 1000)
        simulation = ProtocolSimulation(protocol, start_unix_time=start_time, maximal_current=self._maximal_current)
        run = _Run(run_id=run_id, sample=sample, protocol=protocol, start_time=start_time, simulation=simulation)
        run.points.extend(simulation.advance(0.0))
        self._runs[channel] = run

    @typing.override
    async def stop(self, channel: int) -> None:
        self._runs[channel].state = ChannelState.STOPPED

    @typing.override
    async def pause(self, channel: int) -> None:
        self._runs[channel].state = ChannelState.PAUSED

    @typing.override
    async def resume(self, channel: int) -> None:
        self._runs[channel].state = ChannelState.RUNNING

    @typing.override
    async def clear(self, channel: int) -> None:
        self._runs.pop(channel, None)

    def _status(self, channel: int) -> ChannelStatus:
        run = self._runs.get(channel)
        if channel in self._unavailable or run is None:
            state = ChannelState.UNAVAILABLE if channel in self._unavailable else ChannelState.IDLE
            return ChannelStatus(
                channel=channel,
                state=state,
                voltage=math.nan,
                current=math.nan,
                test_time=0.0,
                cycle_count=0,
                protocol_step=0,
                record_count=0,
            )
        simulation = run.simulation
        return ChannelStatus(
            channel=channel,
            state=run.state,
            voltage=simulation.voltage,
            current=simulation.current if run.state is ChannelState.RUNNING else 0.0,
            test_time=simulation.test_time,
            cycle_count=simulation.cycle_count,
            protocol_step=simulation.protocol_step,
            record_count=len(run.points),
            fault_reason=simulation.fault_reason if run.state is ChannelState.FAULTED else "",
        )

    async def _run_clock(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(self._tick)
            now = loop.time()
            elapsed, last = (now - last) * self._time_scale, now
            for run in self._runs.values():
                if run.state is not ChannelState.RUNNING:
                    continue
                run.points.extend(run.simulation.advance(elapsed))
                if run.simulation.faulted:
                    run.state = ChannelState.FAULTED
                elif run.simulation.finished:
                    run.state = ChannelState.FINISHED
