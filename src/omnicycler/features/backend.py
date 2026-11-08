"""
The Python interface between the standard features and a cycler.

A brand server implements one `CyclerBackend` subclass on top of its own
`io/` layer, and registers the standard features with it (see
`omnicycler.features.server.register_standard_features`). The features never
talk to a device directly.

`CyclerBackend` is a template: brand servers implement the small device
hooks (`read_*`, `start`, `stop`, `pause`, `resume`, `clear`, optionally
`check`), while this base class implements everything that must behave
identically on every cycler: per-channel locking, the channel state machine
including the explicit `ClearChannel` step, the run identity checks, generic
protocol checks against the channel limits, time-range search, monitoring and
the BDF export.

Runs are named by the `RunId` the client chose when it started them. Every
operation on a run and every read of its data names the run it expects; the
base class refuses it with `RunMismatchError` if the channel holds another
run, and with `NoRunError` if it holds none. A run exists from its start until
`ClearChannel` deletes it, data and all.

This module knows nothing about SiLA endpoints; it only uses the shared wire
types.
"""

import abc
import asyncio
import collections
import collections.abc
import dataclasses
import types
import typing

import polars as pl
from aurora_unicycler import ConstantCurrent, ConstantVoltage, CyclingProtocol, VoltageScan

from .bdf_export import encode_frame, records_frame
from .data_table import encode_table
from .errors import (
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
from .protocol import ParsedProtocol, parse_protocol, step_current, technique_steps
from .types import (
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

ACTIVE_STATES = frozenset({ChannelState.RUNNING, ChannelState.PAUSED})
"""States in which a channel executes a protocol."""

ENDED_STATES = frozenset({ChannelState.FINISHED, ChannelState.STOPPED, ChannelState.FAULTED})
"""States in which a channel holds a run that must be cleared before the next one."""

MAXIMAL_RECORDS_PER_RESPONSE = 5000
"""The most data records one response carries; keeps every response well below gRPC's 4 MiB message limit."""

MAXIMAL_RECORDS_PER_TABLE = 100_000
"""The most data records one `GetDataTable` response carries, sent by SiLA binary transfer."""


@dataclasses.dataclass(frozen=True)
class CyclerIdentity:
    """
    The identity of a cycler.

    Attributes:
      manufacturer: The manufacturer of the cycler.
      model: The model of the cycler.
      serial_number: The serial number of the cycler.
      firmware_version: The firmware or vendor software version.
    """

    manufacturer: str
    model: str
    serial_number: str
    firmware_version: str


class UnknownChannelError(ValueError):
    """The channel number does not exist on this cycler."""


class Monitor:
    """
    Streams the new data records of a run in batches.

    Iterate it to receive the batches. Afterwards, `next_index` is the index
    to resume from and `run_ended` tells whether the run has ended and every
    record of it has been delivered. Iteration raises `NoRunError` or
    `RunMismatchError` if the run is cleared before all its records were sent.
    """

    def __init__(  # noqa: PLR0913, PLR0917  # the parameters of MonitorChannel
        self,
        backend: "CyclerBackend",
        channel: int,
        run_id: str,
        from_index: int,
        maximal_duration: float,
        poll_interval: float,
    ) -> None:
        self.next_index = from_index
        self.run_ended = False
        self._backend = backend
        self._channel = channel
        self._run_id = run_id
        self._maximal_duration = maximal_duration
        self._poll_interval = poll_interval

    def __aiter__(self) -> collections.abc.AsyncIterator[list[DataPoint]]:
        return self._batches()

    async def _batches(self) -> collections.abc.AsyncIterator[list[DataPoint]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._maximal_duration
        while True:
            status = await self._backend.require_run(self._channel, self._run_id)
            while self.next_index < status.record_count:
                count = min(MAXIMAL_RECORDS_PER_RESPONSE, status.record_count - self.next_index)
                batch = await self._backend.read_data(self._channel, self._run_id, self.next_index, count)
                if not batch:
                    break
                self.next_index += len(batch)
                yield batch

            if status.state in ENDED_STATES and self.next_index >= status.record_count:
                self.run_ended = True  # an unavailable channel's run may still be running: keep polling
                return
            if loop.time() >= deadline:
                return
            await asyncio.sleep(min(self._poll_interval, max(deadline - loop.time(), 0)))


class CyclerBackend(abc.ABC):
    """
    Base class every brand backend implements.

    Opening a backend connects to the cycler only. It must never start, stop,
    pause or otherwise change a running test, because the connection may be
    reopened at any time.
    """

    def __init__(self) -> None:
        self._locks: collections.defaultdict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self._starting: dict[str, int] = {}  # run IDs of starts in progress, with their channel
        self._identity: CyclerIdentity | None = None
        self._channels: dict[int, ChannelInformation] | None = None
        self._step_types: list[StepType] | None = None

    async def __aenter__(self) -> typing.Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        await self.close()

    # Device hooks, implemented by brand backends.

    async def open(self) -> None:  # noqa: B027  # optional hook
        """Connect to the cycler. Never start, stop or change a test here."""

    async def close(self) -> None:  # noqa: B027  # optional hook
        """Disconnect from the cycler. Running tests keep running."""

    @abc.abstractmethod
    async def read_identity(self) -> CyclerIdentity:
        """Read the identity of the cycler."""

    @abc.abstractmethod
    async def read_channels(self) -> list[ChannelInformation]:
        """Read the static properties of every channel."""

    @abc.abstractmethod
    async def read_supported_step_types(self) -> list[StepType]:
        """Read the protocol step types this cycler can execute."""

    @abc.abstractmethod
    async def read_statuses(self) -> list[ChannelStatus]:
        """
        Read the status of every channel as the device reports it.

        Each status carries the `run_id` of the run the channel holds, or is
        empty without one. A channel holds its run from `start` until `clear`:
        `IDLE` means no run, and an active or ended state always has one. A
        `FAULTED` status carries the cycler's fault message in `fault_reason`,
        translated to plain words where the cycler only gives a code.
        """

    async def read_status(self, channel: int) -> ChannelStatus:
        """
        Read the status of one channel, as `read_statuses` reports it.

        The default reads every channel; override it when one channel is
        cheaper to read, since every data read and monitor poll calls it.

        Raises:
          UnknownChannelError: If the cycler reports no status for the channel.
        """
        for status in await self.read_statuses():
            if status.channel == channel:
                return status
        msg = f"The cycler reported no status for channel {channel}."
        raise UnknownChannelError(msg)

    @abc.abstractmethod
    async def read_run_information(self, channel: int) -> RunInformation:
        """
        Read the run a channel holds, with the `run_id` its start received.

        Raises:
          NoRunError: If the channel holds no run (any more).
        """

    @abc.abstractmethod
    async def read_data(self, channel: int, run_id: str, from_index: int, count: int) -> list[DataPoint]:
        """
        Read data records of a run.

        Args:
          channel: The channel number.
          run_id: The run whose records to read. Return records of this run only, and none if the channel no
            longer holds it.
          from_index: The record index of the first record to return.
          count: The most records to return.

        Returns:
          The records from `from_index` on, at most `count`, fewer at the
          end of the data. A record's index never changes while the run
          exists. Values are converted to BDF units, with positive current
          charging the cell, and NaN for quantities not measured.
        """

    @abc.abstractmethod
    async def start(self, channel: int, protocol: CyclingProtocol, sample: Sample, run_id: str) -> None:
        """
        Start a validated protocol on an idle channel.

        Args:
          channel: The channel number.
          protocol: The protocol, with the sample's name and capacity merged in.
          sample: The cell under test, as the client gave it.
          run_id: The run's identifier. Keep it with the run, so statuses and `read_run_information` report it
            even after the server restarts.
        """

    @abc.abstractmethod
    async def stop(self, channel: int) -> None:
        """Stop the running or paused protocol of a channel."""

    @abc.abstractmethod
    async def pause(self, channel: int) -> None:
        """Pause the running protocol of a channel."""

    @abc.abstractmethod
    async def resume(self, channel: int) -> None:
        """Resume the paused protocol of a channel."""

    @abc.abstractmethod
    async def clear(self, channel: int) -> None:
        """
        Delete the ended run of a channel with all its data, so the channel is idle without a run.

        Release the run on the device too, if the device needs that. The
        vendor's own files may stay; nothing of the run may remain in the
        server, also after a restart.
        """

    async def check(self, channel: int, protocol: CyclingProtocol) -> list[str]:  # noqa: ARG002
        """
        Run brand-specific protocol checks, such as converting it to the vendor format.

        Called after the generic checks passed.

        Returns:
          Warnings for the caller.

        Raises:
          UnsupportedProtocolError: If this cycler cannot execute the protocol.
        """
        return []

    # The interface the standard features use.

    async def get_identity(self) -> CyclerIdentity:
        """Return the identity of the cycler, read once."""
        if self._identity is None:
            self._identity = await self.read_identity()
        return self._identity

    async def get_channels(self) -> list[ChannelInformation]:
        """Return the static properties of every channel, read once."""
        return list((await self._channel_map()).values())

    async def get_channel(self, channel: int) -> ChannelInformation:
        """
        Return the static properties of one channel.

        Raises:
          UnknownChannelError: If the channel does not exist.
        """
        channels = await self._channel_map()
        if channel not in channels:
            msg = f"Channel {channel} does not exist; channels are {sorted(channels)}."
            raise UnknownChannelError(msg)
        return channels[channel]

    async def get_supported_step_types(self) -> list[StepType]:
        """Return the protocol step types this cycler can execute, read once."""
        if self._step_types is None:
            self._step_types = await self.read_supported_step_types()
        return list(self._step_types)

    async def get_channel_statuses(self) -> list[ChannelStatus]:
        """Return the status of every channel."""
        return await self.read_statuses()

    async def get_channel_status(self, channel: int) -> ChannelStatus:
        """
        Return the status of one channel.

        Raises:
          UnknownChannelError: If the channel does not exist.
        """
        await self.get_channel(channel)
        return await self.read_status(channel)

    async def require_run(self, channel: int, run_id: str) -> ChannelStatus:
        """
        Return the status of a channel, checking that it holds the named run.

        Raises:
          NoRunError: If the channel holds no run.
          RunMismatchError: If the channel holds another run.
        """
        status = await self.get_channel_status(channel)
        _check_run(status, run_id)
        return status

    async def get_run_information(self, channel: int) -> RunInformation:
        """
        Return the run a channel holds.

        Raises:
          NoRunError: If the channel holds no run.
        """
        if not (await self.get_channel_status(channel)).run_id:
            msg = f"Channel {channel} holds no run."
            raise NoRunError(msg)
        return await self.read_run_information(channel)

    async def get_record_count(self, channel: int, run_id: str) -> int:
        """
        Return the number of records of a run.

        Raises:
          NoRunError: If the channel holds no run.
          RunMismatchError: If the channel holds another run.
        """
        return (await self.require_run(channel, run_id)).record_count

    async def get_data(self, channel: int, run_id: str, from_index: int, count: int) -> list[DataPoint]:
        """
        Return up to `count` records of a run from `from_index` on.

        Raises:
          NoRunError: If the channel holds no run.
          RunMismatchError: If the channel holds another run.
        """
        await self.require_run(channel, run_id)
        count = min(count, MAXIMAL_RECORDS_PER_RESPONSE)
        if count <= 0:
            return []
        points = await self.read_data(channel, run_id, from_index, count)
        if len(points) < count:
            await self.require_run(channel, run_id)  # a short page must not hide a run cleared meanwhile
        return points

    async def get_data_table(self, channel: int, run_id: str, from_index: int, count: int, labels: list[str]) -> bytes:
        """
        Return up to `count` records of a run from `from_index` on as an Arrow IPC stream; see `data_table`.

        Brand backends may override this, for example to read the vendor's
        data file in bulk.

        Raises:
          NoRunError: If the channel holds no run.
          RunMismatchError: If the channel holds another run.
        """
        await self.require_run(channel, run_id)
        points: list[DataPoint] = []
        count = min(count, MAXIMAL_RECORDS_PER_TABLE)
        while len(points) < count:
            want = min(count - len(points), MAXIMAL_RECORDS_PER_RESPONSE)
            batch = await self.read_data(channel, run_id, from_index + len(points), want)
            points.extend(batch)
            if len(batch) < want:
                break
        await self.require_run(channel, run_id)
        return encode_table(points, labels)

    async def get_data_in_time_range(
        self, channel: int, run_id: str, from_test_time: float, to_test_time: float
    ) -> list[DataPoint]:
        """
        Return the records of a run whose test time lies within an inclusive range.

        Brand backends may override this with a faster query; the default
        bisects the record index, relying on test time never decreasing.

        Raises:
          NoRunError: If the channel holds no run.
          RunMismatchError: If the channel holds another run.
          DataRangeTooLargeError: If more than `MAXIMAL_RECORDS_PER_RESPONSE` records match.
        """
        status = await self.require_run(channel, run_id)
        index = await self._first_index_at(channel, run_id, from_test_time, status.record_count)
        points: list[DataPoint] = []
        while index < status.record_count:
            batch = await self.read_data(channel, run_id, index, MAXIMAL_RECORDS_PER_RESPONSE)
            if not batch:
                break
            for point in batch:
                if point.test_time > to_test_time:
                    await self.require_run(channel, run_id)
                    return points
                points.append(point)
                if len(points) > MAXIMAL_RECORDS_PER_RESPONSE:
                    msg = (
                        f"More than {MAXIMAL_RECORDS_PER_RESPONSE} records lie between test times "
                        f"{from_test_time} s and {to_test_time} s."
                    )
                    raise DataRangeTooLargeError(msg)
            index += len(batch)
        await self.require_run(channel, run_id)
        return points

    def monitor(
        self, channel: int, run_id: str, from_index: int, maximal_duration: float, poll_interval: float = 1.0
    ) -> Monitor:
        """
        Stream new records of a run until it ends or `maximal_duration` seconds pass.

        Returns:
          A `Monitor` to iterate for batches of records.
        """
        return Monitor(self, channel, run_id, from_index, maximal_duration, poll_interval)

    async def export_data(self, channel: int, run_id: str, data_format: BdfFormat) -> BdfExport:
        """
        Export a whole run as a Battery Data Format file with its BattINFO metadata sidecar.

        The records are converted to a table page by page, so a long run
        never sits in memory as `DataPoint` objects. Brand backends may
        override this, for example to convert the vendor's native data file;
        `bdf_metadata.build_metadata` and `bdf_export.encode_frame` then
        still give the same sidecar and checks.

        Raises:
          NoRunError: If the channel holds no run, also if it was cleared during the export.
          RunMismatchError: If the channel holds another run.
        """
        from .bdf_metadata import build_metadata  # noqa: PLC0415  # it imports this module

        await self.require_run(channel, run_id)
        run = await self.read_run_information(channel)
        frames: list[pl.DataFrame] = []
        last: DataPoint | None = None
        index = 0
        while batch := await self.read_data(channel, run_id, index, MAXIMAL_RECORDS_PER_RESPONSE):
            frames.append(records_frame(batch))
            last = batch[-1]
            index += len(batch)
        status = await self.require_run(channel, run_id)  # an export cut short by a clear is refused
        metadata = build_metadata(
            identity=await self.get_identity(),
            channel=await self.get_channel(channel),
            run=run,
            status=status,
            last_point=last,
        )
        frame = pl.concat(frames) if frames else records_frame([])
        return encode_frame(frame, data_format, metadata)

    async def check_protocol(self, channel: int, protocol: CyclingProtocol) -> list[str]:
        """
        Check that a channel of this cycler can execute a protocol.

        Checks the step types and the channel's voltage and current limits,
        then the brand's own checks.

        Returns:
          Warnings for the caller.

        Raises:
          UnsupportedProtocolError: If the channel cannot execute the protocol.
        """
        information = await self.get_channel(channel)
        supported = set(await self.get_supported_step_types())
        nominal_capacity = (protocol.sample.capacity_mAh or 0.0) / 1000

        for position, step, kind in technique_steps(protocol):
            if kind not in supported:
                names = ", ".join(sorted(_wire_name(item) for item in supported))
                msg = f"Step {position} is {_wire_name(kind)!r}, which this cycler cannot execute; it supports {names}."
                raise UnsupportedProtocolError(msg)
            if isinstance(step, ConstantCurrent):
                current = step_current(step, nominal_capacity)
                if abs(current) > information.maximal_current:
                    msg = (
                        f"Step {position} needs {abs(current):g} A, more than channel {channel}'s "
                        f"{information.maximal_current:g} A."
                    )
                    raise UnsupportedProtocolError(msg)
                _check_voltage(position, step.until_voltage_V, information)
            if isinstance(step, ConstantVoltage):
                _check_voltage(position, step.voltage_V, information)
            if isinstance(step, VoltageScan):
                _check_voltage(position, step.start_voltage_V, information)
                _check_voltage(position, step.end_voltage_V, information)

        return await self.check(channel, protocol)

    async def prepare_protocol(self, channel: int, data: bytes, sample: Sample) -> ParsedProtocol:
        """
        Decode a protocol from the wire, merge the sample in and check it against this cycler.

        Returns:
          The protocol with every warning of decoding and checking.

        Raises:
          InvalidSampleError: If the sample cannot be used.
          InvalidProtocolError: If the bytes are not a valid aurora-unicycler protocol.
          UnsupportedProtocolError: If the channel cannot execute the protocol.
        """
        parsed = parse_protocol(data, sample)
        warnings = await self.check_protocol(channel, parsed.protocol)
        return dataclasses.replace(parsed, warnings=[*parsed.warnings, *warnings])

    async def start_protocol(self, channel: int, run_id: str, protocol: CyclingProtocol, sample: Sample) -> None:
        """
        Start a validated protocol on an idle channel as the run `run_id`; see `start_run`.

        Raises:
          ChannelBusyError: If the channel is running or paused another run.
          ChannelNotClearedError: If the channel holds another, ended run.
          ChannelUnavailableError: If the channel is unavailable.
          RunIdInUseError: If another channel holds a run with this identifier.
        """
        await self.start_run(channel, run_id, lambda: self.start(channel, protocol, sample, run_id))

    async def start_run(
        self, channel: int, run_id: str, start: collections.abc.Callable[[], collections.abc.Awaitable[None]]
    ) -> None:
        """
        Start a run on an idle channel, with the checks every start makes; brand features use it for native starts.

        A start is idempotent: if the channel already holds the run `run_id`,
        it succeeds without effect, so a client may repeat a start whose
        outcome it does not know.

        Args:
          channel: The channel number.
          run_id: The identifier the client chose for the run.
          start: Starts the run on the device; called with the channel's lock held, after every check passed.

        Raises:
          ChannelBusyError: If the channel is running or paused another run.
          ChannelNotClearedError: If the channel holds another, ended run.
          ChannelUnavailableError: If the channel is unavailable.
          RunIdInUseError: If another channel holds a run with this identifier.
        """
        async with self._locks[channel]:
            status = await self.get_channel_status(channel)
            if status.run_id == run_id:
                return  # started before: a repeated start succeeds without effect
            if self._starting.get(run_id, channel) != channel:
                msg = f"Run {run_id!r} is being started on channel {self._starting[run_id]}; give each run its own ID."
                raise RunIdInUseError(msg)
            self._starting[run_id] = channel
            try:
                for other in await self.get_channel_statuses():
                    if other.run_id == run_id and other.channel != channel:
                        msg = f"Channel {other.channel} holds run {run_id!r}; give each run its own ID."
                        raise RunIdInUseError(msg)
                if status.state in ACTIVE_STATES:
                    msg = f"Channel {channel} is {_wire_name(status.state)}; stop its protocol first."
                    raise ChannelBusyError(msg)
                if status.state in ENDED_STATES:
                    msg = f"Channel {channel} holds a {_wire_name(status.state)} run; clear the channel first."
                    raise ChannelNotClearedError(msg)
                _require_available(channel, status.state)
                if status.run_id:
                    msg = f"Channel {channel} holds run {status.run_id!r}; clear the channel first."
                    raise ChannelNotClearedError(msg)
                await start()
            finally:
                del self._starting[run_id]

    async def stop_protocol(self, channel: int, run_id: str) -> None:
        """
        Stop a run's protocol. Succeeds without effect if it is not running or paused, or the channel holds no run.

        Raises:
          RunMismatchError: If the channel holds another run.
        """
        async with self._locks[channel]:
            status = await self.get_channel_status(channel)
            if not status.run_id:
                return
            _check_run(status, run_id)
            if status.state in ACTIVE_STATES:
                await self.stop(channel)

    async def pause_protocol(self, channel: int, run_id: str) -> None:
        """
        Pause a run's protocol. Succeeds without effect if it is already paused.

        Raises:
          RunMismatchError: If the channel holds another run.
          NoActiveProtocolError: If the channel has no running or paused protocol.
        """
        async with self._locks[channel]:
            status = await self.get_channel_status(channel)
            _check_held_run(status, run_id)
            _require_active(channel, status.state)
            if status.state is ChannelState.RUNNING:
                await self.pause(channel)

    async def resume_protocol(self, channel: int, run_id: str) -> None:
        """
        Resume a run's protocol. Succeeds without effect if it is already running.

        Raises:
          RunMismatchError: If the channel holds another run.
          NoActiveProtocolError: If the channel has no running or paused protocol.
        """
        async with self._locks[channel]:
            status = await self.get_channel_status(channel)
            _check_held_run(status, run_id)
            _require_active(channel, status.state)
            if status.state is ChannelState.PAUSED:
                await self.resume(channel)

    async def clear_channel(self, channel: int, run_id: str) -> None:
        """
        Delete a run that has ended, with all its data, so the channel is idle. Succeeds without effect if idle.

        Raises:
          RunMismatchError: If the channel holds another run.
          ChannelBusyError: If the run is running or paused.
          ChannelUnavailableError: If the channel is unavailable.
        """
        async with self._locks[channel]:
            status = await self.get_channel_status(channel)
            if not status.run_id:
                return
            _check_run(status, run_id)
            if status.state in ACTIVE_STATES:
                msg = f"Channel {channel} is {_wire_name(status.state)}; stop its protocol before clearing it."
                raise ChannelBusyError(msg)
            _require_available(channel, status.state)
            await self.clear(channel)

    # Helpers.

    async def _channel_map(self) -> dict[int, ChannelInformation]:
        if self._channels is None:
            self._channels = {information.channel: information for information in await self.read_channels()}
        return self._channels

    async def _first_index_at(self, channel: int, run_id: str, test_time: float, record_count: int) -> int:
        low, high = 0, record_count
        while low < high:
            middle = (low + high) // 2
            point = await self.read_data(channel, run_id, middle, 1)
            if not point:
                high = middle
            elif point[0].test_time < test_time:
                low = middle + 1
            else:
                high = middle
        return low


def _wire_name(member: StepType | ChannelState) -> str:
    return member.name.lower().replace("_", " ")


def _check_run(status: ChannelStatus, run_id: str) -> None:
    if not status.run_id:
        msg = f"Channel {status.channel} holds no run; run {run_id!r} was cleared or never started."
        raise NoRunError(msg)
    _check_held_run(status, run_id)


def _check_held_run(status: ChannelStatus, run_id: str) -> None:
    if status.run_id and status.run_id != run_id:
        msg = f"Channel {status.channel} holds run {status.run_id!r}, not {run_id!r}."
        raise RunMismatchError(msg)


def _require_available(channel: int, state: ChannelState) -> None:
    if state is ChannelState.UNAVAILABLE:
        msg = f"Channel {channel} is unavailable."
        raise ChannelUnavailableError(msg)


def _require_active(channel: int, state: ChannelState) -> None:
    if state not in ACTIVE_STATES:
        msg = f"Channel {channel} is {_wire_name(state)} and has no running or paused protocol."
        raise NoActiveProtocolError(msg)


def _check_voltage(position: int, voltage: float | None, information: ChannelInformation) -> None:
    if voltage is None:
        return
    if not information.minimal_voltage <= voltage <= information.maximal_voltage:
        msg = (
            f"Step {position} uses {voltage:g} V, outside channel {information.channel}'s range of "
            f"{information.minimal_voltage:g} V to {information.maximal_voltage:g} V."
        )
        raise UnsupportedProtocolError(msg)


__all__ = [
    "ACTIVE_STATES",
    "ENDED_STATES",
    "MAXIMAL_RECORDS_PER_RESPONSE",
    "MAXIMAL_RECORDS_PER_TABLE",
    "CyclerBackend",
    "CyclerIdentity",
    "Monitor",
    "UnknownChannelError",
]
