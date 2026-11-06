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
    UnsupportedProtocolError,
)
from ..parameters import ChannelNumber, ProtocolDocument, restrict_channels
from ..types import ChannelStatus, RunInformation, Sample


class ChannelController(sila.Feature):
    """
    Run cycling protocols on the channels of the battery cycler and follow their state.

    A protocol is an aurora-unicycler protocol, started on one channel with `StartProtocol`. A channel moves from
    `idle` to `running`, can be paused and resumed, and ends as `finished`, `stopped` or `faulted`. An ended run
    stays on the channel, with its data available through `MeasurementProvider`, until `ClearChannel` is called;
    only then does the channel return to `idle` and accept a new protocol. `ChannelStatuses` reports every state
    change. Commands on different channels may run concurrently.
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
        Get the sample and protocol of the run a channel holds.

        Args:
          Channel: The channel number.

        Returns:
          RunInformation: The run the channel holds.
        """
        return await self.backend.get_run_information(channel)

    @sila.UnobservableCommand(errors=[ChannelBusyError, ChannelNotClearedError, ChannelUnavailableError])
    async def start_protocol(self, channel: ChannelNumber, protocol: ProtocolDocument, sample: Sample) -> None:
        """
        Start a protocol on an idle channel.

        The sample replaces the protocol's own sample block, so one protocol serves many cells, and its nominal
        capacity is the reference for C-rates. The protocol is checked before anything happens on the channel:
        a protocol that is invalid, or that this cycler or channel cannot execute, is rejected as a validation
        error. Once started, the channel reports `running`.

        Args:
          Channel: The channel to start the protocol on.
          Protocol: The protocol as UTF-8 encoded aurora-unicycler JSON.
          Sample: The cell under test.
        """
        prepared = await self.backend.prepare_protocol(channel, protocol, sample)
        await self.backend.start_protocol(channel, prepared.protocol)

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

    @sila.UnobservableCommand()
    async def stop_protocol(self, channel: ChannelNumber) -> None:
        """
        Stop the protocol of a channel; the channel then reports `stopped`.

        Succeeds without effect if the channel has no running or paused protocol.

        Args:
          Channel: The channel number.
        """
        await self.backend.stop_protocol(channel)

    @sila.UnobservableCommand(errors=[NoActiveProtocolError])
    async def pause_protocol(self, channel: ChannelNumber) -> None:
        """
        Pause the running protocol of a channel; the channel then reports `paused`.

        Succeeds without effect if the protocol is already paused.

        Args:
          Channel: The channel number.
        """
        await self.backend.pause_protocol(channel)

    @sila.UnobservableCommand(errors=[NoActiveProtocolError])
    async def resume_protocol(self, channel: ChannelNumber) -> None:
        """
        Resume the paused protocol of a channel; the channel then reports `running`.

        Succeeds without effect if the protocol is already running.

        Args:
          Channel: The channel number.
        """
        await self.backend.resume_protocol(channel)

    @sila.UnobservableCommand(errors=[ChannelBusyError, ChannelUnavailableError])
    async def clear_channel(self, channel: ChannelNumber) -> None:
        """
        Release the ended run of a channel so it returns to `idle` and accepts a new protocol.

        Retrieve the run's data before clearing: afterwards it is no longer available through this server. The
        data stays on the cycler itself. Succeeds without effect if the channel is already idle.

        Args:
          Channel: The channel number.
        """
        await self.backend.clear_channel(channel)

    @validate_protocol.validator
    @start_protocol.validator
    async def _check_protocol(self, channel: int, protocol: bytes, sample: Sample) -> None:
        try:
            await self.backend.prepare_protocol(channel, protocol, sample)
        except InvalidSampleError as error:
            raise sila.errors.ValidationError(str(error), "sample") from None
        except (InvalidProtocolError, UnsupportedProtocolError) as error:
            raise sila.errors.ValidationError(str(error), "protocol") from None
