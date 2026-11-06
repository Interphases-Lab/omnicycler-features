"""Exceptions raised by the typed client in addition to the shared feature errors."""


class OmniCyclerClientError(Exception):
    """Base class of the client's own exceptions."""


class IncompatibleServerError(OmniCyclerClientError):
    """The server lacks a standard feature, or offers it at an unexpected major version."""


class ProtocolMismatchError(OmniCyclerClientError):
    """The server sent a value the client does not expect. This is a bug in the client/server pair."""


class DeviceError(OmniCyclerClientError):
    """The server reported an unexpected error, typically a hardware or vendor software failure."""


class CommandOutcomeUnknownError(OmniCyclerClientError):
    """
    The connection failed after a command was sent, so whether it took effect is unknown.

    Check the channel state before retrying.
    """


class InvalidParameterError(OmniCyclerClientError, ValueError):
    """
    The server rejected a parameter before executing the command.

    Attributes:
      parameter: The SiLA identifier of the rejected parameter, e.g. `Protocol`.
    """

    def __init__(self, message: str, parameter: str) -> None:
        super().__init__(message)
        self.parameter = parameter


__all__ = [
    "CommandOutcomeUnknownError",
    "DeviceError",
    "IncompatibleServerError",
    "InvalidParameterError",
    "OmniCyclerClientError",
    "ProtocolMismatchError",
]
