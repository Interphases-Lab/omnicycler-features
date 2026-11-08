import asyncio
import collections.abc
import contextlib
import uuid

from aurora_unicycler import CyclingProtocol
from sila.client import FeatureNamespace

from .. import CATEGORY, ORIGINATOR
from ..errors import (
    ChannelBusyError,
    ChannelNotClearedError,
    ChannelUnavailableError,
    NoActiveProtocolError,
    NoRunError,
    RunIdInUseError,
    RunMismatchError,
)
from ..protocol import encode_protocol
from ..types import ChannelState, ChannelStatus, RunInformation, Sample
from .base import ErrorMap, error_map, from_wire, list_from_wire, ms, response, to_wire, translate_errors

FEATURE = f"{ORIGINATOR}/{CATEGORY}/ChannelController/v1"
ERRORS: ErrorMap = error_map(
    ChannelBusyError,
    ChannelNotClearedError,
    ChannelUnavailableError,
    NoActiveProtocolError,
    NoRunError,
    RunIdInUseError,
    RunMismatchError,
)


class ChannelControllerClient:
    """Typed client of `ChannelController`: run protocols on channels and follow their state."""

    def __init__(self, namespace: FeatureNamespace) -> None:
        self._ns = namespace

    @contextlib.asynccontextmanager
    async def subscribe_channel_statuses(
        self,
    ) -> collections.abc.AsyncGenerator[collections.abc.AsyncIterator[list[ChannelStatus]]]:
        """Subscribe to the statuses of all channels; the subscription closes when the context exits."""
        with translate_errors(ERRORS):
            stream = await self._ns.ChannelStatuses.subscribe()

        async def values() -> collections.abc.AsyncIterator[list[ChannelStatus]]:
            async for raw in stream:
                yield list_from_wire(ChannelStatus, raw)

        try:
            yield values()
        finally:
            stream.close()

    async def get_channel_status(self, channel: int, *, timeout: float | None = None) -> ChannelStatus:
        """Get the current status of one channel."""
        with translate_errors(ERRORS):
            responses = await self._ns.GetChannelStatus(Channel=channel, timeout=ms(timeout))
        return from_wire(ChannelStatus, response(responses, "Status"))

    async def get_run_information(self, channel: int, *, timeout: float | None = None) -> RunInformation:
        """
        Get the sample and protocol of the run a channel holds.

        Raises:
          NoRunError: If the channel holds no run.
        """
        with translate_errors(ERRORS):
            responses = await self._ns.GetRunInformation(Channel=channel, timeout=ms(timeout))
        return from_wire(RunInformation, response(responses, "RunInformation"))

    async def start_protocol(
        self,
        channel: int,
        protocol: CyclingProtocol | bytes,
        sample: Sample,
        *,
        run_id: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """
        Start a protocol on an idle channel, as a new run.

        If the outcome is unknown (`CommandOutcomeUnknownError`), call again with the same `run_id`: a start of
        a run the channel already holds succeeds without effect.

        Args:
          channel: The channel number.
          protocol: An aurora-unicycler protocol, or its UTF-8 encoded JSON.
          sample: The cell under test; replaces the protocol's own sample block.
          run_id: The new run's identifier; a fresh UUID if omitted.
          timeout: The longest to wait for the server, in seconds.

        Returns:
          The run's identifier.

        Raises:
          InvalidParameterError: If the protocol or sample is rejected; `parameter` names which.
          ChannelBusyError: If the channel is running or paused another run.
          ChannelNotClearedError: If the channel holds another, ended run.
          ChannelUnavailableError: If the channel is unavailable.
          RunIdInUseError: If another channel holds a run with this identifier.
        """
        run_id = run_id or str(uuid.uuid4())
        with translate_errors(ERRORS):
            await self._ns.StartProtocol(
                Channel=channel, RunId=run_id, Protocol=_encode(protocol), Sample=to_wire(sample), timeout=ms(timeout)
            )
        return run_id

    async def validate_protocol(
        self, channel: int, protocol: CyclingProtocol | bytes, sample: Sample, *, timeout: float | None = None
    ) -> list[str]:
        """
        Check a protocol exactly as `start_protocol` would, without starting it.

        Returns:
          Warnings that do not prevent the protocol from starting.

        Raises:
          InvalidParameterError: If the protocol or sample is rejected; `parameter` names which.
        """
        with translate_errors(ERRORS):
            responses = await self._ns.ValidateProtocol(
                Channel=channel, Protocol=_encode(protocol), Sample=to_wire(sample), timeout=ms(timeout)
            )
        return list_from_wire(str, response(responses, "Warnings"))

    async def stop_protocol(self, channel: int, run_id: str, *, timeout: float | None = None) -> None:
        """
        Stop the protocol of a run. Succeeds without effect if it is not running or paused, or the channel holds no run.

        Raises:
          RunMismatchError: If the channel holds another run.
        """
        with translate_errors(ERRORS):
            await self._ns.StopProtocol(Channel=channel, RunId=run_id, timeout=ms(timeout))

    async def pause_protocol(self, channel: int, run_id: str, *, timeout: float | None = None) -> None:
        """
        Pause the running protocol of a run. Succeeds without effect if already paused.

        Raises:
          RunMismatchError: If the channel holds another run.
          NoActiveProtocolError: If the channel has no running or paused protocol.
        """
        with translate_errors(ERRORS):
            await self._ns.PauseProtocol(Channel=channel, RunId=run_id, timeout=ms(timeout))

    async def resume_protocol(self, channel: int, run_id: str, *, timeout: float | None = None) -> None:
        """
        Resume the paused protocol of a run. Succeeds without effect if already running.

        Raises:
          RunMismatchError: If the channel holds another run.
          NoActiveProtocolError: If the channel has no running or paused protocol.
        """
        with translate_errors(ERRORS):
            await self._ns.ResumeProtocol(Channel=channel, RunId=run_id, timeout=ms(timeout))

    async def clear_channel(self, channel: int, run_id: str, *, timeout: float | None = None) -> None:
        """
        Delete an ended run with all its data, so the channel is idle. Export the data first: it is gone for good.

        Succeeds without effect if the channel holds no run.

        Raises:
          RunMismatchError: If the channel holds another run.
          ChannelBusyError: If the run is running or paused.
          ChannelUnavailableError: If the channel is unavailable.
        """
        with translate_errors(ERRORS):
            await self._ns.ClearChannel(Channel=channel, RunId=run_id, timeout=ms(timeout))

    async def wait_for_state(
        self, channel: int, states: collections.abc.Collection[ChannelState], *, timeout: float
    ) -> ChannelStatus:
        """
        Wait until a channel reaches one of the given states.

        Args:
          channel: The channel number.
          states: The states to wait for.
          timeout: The longest to wait, in seconds.

        Returns:
          The first status of the channel in one of the states.

        Raises:
          TimeoutError: If the channel does not reach the states in time.
        """
        async with asyncio.timeout(timeout), self.subscribe_channel_statuses() as statuses:
            async for snapshot in statuses:
                for status in snapshot:
                    if status.channel == channel and status.state in states:
                        return status
        msg = f"The status subscription ended before channel {channel} reached {sorted(s.name for s in states)}."
        raise TimeoutError(msg)


def _encode(protocol: CyclingProtocol | bytes) -> bytes:
    return protocol if isinstance(protocol, bytes) else encode_protocol(protocol)
