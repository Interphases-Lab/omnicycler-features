"""
Errors of the OmniCycler standard features.

The execution errors below are declared on the SiLA endpoints, so a client
receives them as Defined Execution Errors. The typed client maps them back
to these same classes, so `except ChannelBusyError` works identically on the
server and on the client.

`InvalidProtocolError`, `UnsupportedProtocolError` and `InvalidSampleError`
are never sent as such: the features turn them into SiLA validation errors on
the `Protocol` or `Sample` parameter.
"""


class ChannelBusyError(Exception):
    """
    The channel is running or paused a protocol.

    Stop the protocol with `ChannelController.StopProtocol`, or wait until it
    finishes, before retrying.
    """


class ChannelNotClearedError(Exception):
    """
    The channel still holds a finished, stopped or faulted run.

    Retrieve the data of the run, then call `ChannelController.ClearChannel`
    before starting a new protocol.
    """


class ChannelUnavailableError(Exception):
    """
    The channel cannot be used, for example because it is offline or not installed.

    Check the cycler and its connection; `ChannelController.ChannelStatuses`
    reports when the channel becomes available again.
    """


class NoActiveProtocolError(Exception):
    """
    The channel has no running or paused protocol to act on.

    Check the channel state with `ChannelController.GetChannelStatus`.
    """


class NoRunError(Exception):
    """
    The channel holds no run.

    The run was cleared, or none was started. Start a protocol with
    `ChannelController.StartProtocol` first.
    """


class RunMismatchError(Exception):
    """
    The channel holds a different run than the one named.

    The run you meant has ended and been cleared, and another run now holds
    the channel. Read `ChannelStatus.RunId` and act on that run only if you
    mean to.
    """


class RunIdInUseError(Exception):
    """
    Another channel already holds a run with this RunId.

    Give every run its own identifier, for example a fresh UUID.
    """


class DataRangeTooLargeError(Exception):
    """
    The requested time range contains more records than one response may carry.

    Request a shorter time range, or page through the data by record index
    with `MeasurementProvider.GetData`.
    """


class InvalidProtocolError(ValueError):
    """The protocol is not a valid aurora-unicycler protocol."""


class UnsupportedProtocolError(ValueError):
    """The protocol is valid, but this cycler or channel cannot execute it."""


class InvalidSampleError(ValueError):
    """The sample description cannot be used, for example because its name is a placeholder."""


__all__ = [
    "ChannelBusyError",
    "ChannelNotClearedError",
    "ChannelUnavailableError",
    "DataRangeTooLargeError",
    "InvalidProtocolError",
    "InvalidSampleError",
    "NoActiveProtocolError",
    "NoRunError",
    "RunIdInUseError",
    "RunMismatchError",
    "UnsupportedProtocolError",
]
