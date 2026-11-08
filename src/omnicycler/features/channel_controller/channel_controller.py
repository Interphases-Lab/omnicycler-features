import typing

from unitelabs.cdk import Publisher, Replay, sila

from .. import CATEGORY, ORIGINATOR
from ..backend import CyclerBackend
from ..errors import (
    ChannelBusyError,
    ChannelNotClearedError,
    ChannelUnavailableError,
    InvalidProtocolError,
    InvalidSampleError,
    NoActiveProtocolError,
    NoRunError,
    RunIdInUseError,
    RunMismatchError,
    UnsupportedProtocolError,
)
from ..parameters import ChannelNumber, ProtocolDocument, RunIdentifier, restrict_channels
from ..types import ChannelStatus, RunInformation, Sample


class ChannelController(sila.Feature):
    """
    Run cycling protocols on the channels of the battery cycler and follow their state.

    A protocol is an aurora-unicycler protocol, started on one channel with `StartProtocol` as a run whose `RunId`
    the client chooses. A channel moves from `idle` to `running`, can be paused and resumed, and ends as
    `finished`, `stopped` or `faulted`. An ended run stays on the channel, with its data available through
    `MeasurementProvider`, until `ClearChannel` deletes it; only then does the channel return to `idle` and accept
    a new protocol. Every command that acts on a run names its `RunId`, and is refused with `RunMismatchError` if
    the channel holds another run, so a client never acts on a run it does not mean. `ChannelStatuses` reports
    every state change, with the run each channel holds. Commands on different channels may run concurrently.
    """

    def __init__(self, backend: CyclerBackend, *, status_interval: float = 1.0) -> None:
        super().__init__(originator=ORIGINATOR, category=CATEGORY)
        self.backend = backend
        self._statuses = Publisher[list[ChannelStatus], list[ChannelStatus]](
            source=backend.get_channel_statuses, interval=status_interval, maxsize=1
        )

    @typing.override
    async def on_before_start(self) -> None:
        channels = [information.channel for information in await self.backend.get_channels()]
        handlers = [
            self.get_channel_status,
            self.get_run_information,
            self.start_protocol,
            self.validate_protocol,
            self.stop_protocol,
            self.pause_protocol,
            self.resume_protocol,
            self.clear_channel,
        ]
        restrict_channels(self, handlers, channels)

    @sila.ObservableProperty()
    async def subscribe_channel_statuses(self) -> sila.Stream[list[ChannelStatus]]:
        """The status of every channel, sent whenever one of them changes."""
        return self._statuses.subscribe(replay=Replay.CURRENT)

    @sila.UnobservableCommand()
    async def get_channel_status(self, channel: ChannelNumber) -> ChannelStatus:
        """
        Get the current status of one channel.

        Args:
          Channel: The channel number.

        Returns:
          Status: The status of the channel.
        """
        return await self.backend.get_channel_status(channel)

    @sila.UnobservableCommand(errors=[NoRunError])
    async def get_run_information(self, channel: ChannelNumber) -> RunInformation:
        """
        Get the run a channel holds: its RunId, sample and protocol.

        Args:
          Channel: The channel number.

        Returns:
          RunInformation: The run the channel holds.
        """
        return await self.backend.get_run_information(channel)

    @sila.UnobservableCommand(
        errors=[ChannelBusyError, ChannelNotClearedError, ChannelUnavailableError, RunIdInUseError]
    )
    async def start_protocol(
        self, channel: ChannelNumber, run_id: RunIdentifier, protocol: ProtocolDocument, sample: Sample
    ) -> None:
        """
        Start a protocol on an idle channel, as a new run.

        The sample replaces the protocol's own sample block, so one protocol serves many cells, and its nominal
        capacity is the reference for C-rates. The protocol is checked before anything happens on the channel:
        a protocol that is invalid, or that this cycler or channel cannot execute, is rejected as a validation
        error. Once started, the channel reports `running` and the run's RunId.

        Starting is idempotent: if the channel already holds the run RunId, the command succeeds without effect.
        A client that does not know whether a start took effect, for example after a lost connection, repeats it
        with the same RunId.

        Args:
          Channel: The channel to start the protocol on.
          RunId: The identifier of the new run, unique across runs. Use a fresh UUID unless you have your own.
          Protocol: The protocol as UTF-8 encoded aurora-unicycler JSON.
          Sample: The cell under test.
        """
        prepared = await self.backend.prepare_protocol(channel, protocol, sample)
        await self.backend.start_protocol(channel, run_id, prepared.protocol, sample)

    @sila.UnobservableCommand()
    async def validate_protocol(self, channel: ChannelNumber, protocol: ProtocolDocument, sample: Sample) -> list[str]:
        """
        Check a protocol exactly as `StartProtocol` would, without starting it.

        A protocol that cannot be started is rejected as a validation error.

        Args:
          Channel: The channel the protocol is meant for.
          Protocol: The protocol as UTF-8 encoded aurora-unicycler JSON.
          Sample: The cell under test.

        Returns:
          Warnings: Issues that do not prevent the protocol from starting.
        """
        return (await self.backend.prepare_protocol(channel, protocol, sample)).warnings

    @sila.UnobservableCommand(errors=[RunMismatchError])
    async def stop_protocol(self, channel: ChannelNumber, run_id: RunIdentifier) -> None:
        """
        Stop the protocol of a run; the channel then reports `stopped`.

        Succeeds without effect if the run is not running or paused, or the channel holds no run.

        Args:
          Channel: The channel number.
          RunId: The run to stop.
        """
        await self.backend.stop_protocol(channel, run_id)

    @sila.UnobservableCommand(errors=[NoActiveProtocolError, RunMismatchError])
    async def pause_protocol(self, channel: ChannelNumber, run_id: RunIdentifier) -> None:
        """
        Pause the running protocol of a run; the channel then reports `paused`.

        Succeeds without effect if the protocol is already paused.

        Args:
          Channel: The channel number.
          RunId: The run to pause.
        """
        await self.backend.pause_protocol(channel, run_id)

    @sila.UnobservableCommand(errors=[NoActiveProtocolError, RunMismatchError])
    async def resume_protocol(self, channel: ChannelNumber, run_id: RunIdentifier) -> None:
        """
        Resume the paused protocol of a run; the channel then reports `running`.

        Succeeds without effect if the protocol is already running.

        Args:
          Channel: The channel number.
          RunId: The run to resume.
        """
        await self.backend.resume_protocol(channel, run_id)

    @sila.UnobservableCommand(errors=[ChannelBusyError, ChannelUnavailableError, RunMismatchError])
    async def clear_channel(self, channel: ChannelNumber, run_id: RunIdentifier) -> None:
        """
        Delete an ended run with all its data, so the channel is `idle` and accepts a new protocol.

        Export the run's data first: afterwards it is gone from this server for good. Succeeds without effect if
        the channel holds no run.

        Args:
          Channel: The channel number.
          RunId: The run to delete.
        """
        await self.backend.clear_channel(channel, run_id)

    @validate_protocol.validator
    @start_protocol.validator
    async def _check_protocol(self, channel: int, protocol: bytes, sample: Sample) -> None:
        try:
            await self.backend.prepare_protocol(channel, protocol, sample)
        except InvalidSampleError as error:
            raise sila.errors.ValidationError(str(error), "sample") from None
        except (InvalidProtocolError, UnsupportedProtocolError) as error:
            raise sila.errors.ValidationError(str(error), "protocol") from None
