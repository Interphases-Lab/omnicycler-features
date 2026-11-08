"""
Connection and conversion helpers of the typed client.

All access to the dynamically typed `sila.client` API is confined to this
module and the per-feature client modules. Public client methods only ever
accept and return the shared wire types.

This module is public: brand repos build the clients of their brand features
on it, so every OmniCycler client decodes the wire and translates errors the
same way.
"""

import collections.abc
import contextlib
import dataclasses
import enum
import typing

from sila.client import (
    Client,
    CommandExecution,
    CommandExecutionNotAccepted,
    DefinedExecutionError,
    FeatureNamespace,
    SiLAConnectionError,
    SiLATimeoutError,
    UndefinedExecutionError,
    ValidationError,
)

from .errors import (
    CommandOutcomeUnknownError,
    DeviceError,
    IncompatibleServerError,
    InvalidParameterError,
    ProtocolMismatchError,
)

if typing.TYPE_CHECKING:
    from _typeshed import DataclassInstance

ErrorMap = collections.abc.Mapping[str, type[Exception]]


def pascal(name: str) -> str:
    """Convert a Python name to its SiLA identifier, e.g. `nominal_capacity` to `NominalCapacity`."""
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def wire_name(member: enum.Enum) -> str:
    """Return the wire string of an enum member, e.g. `NOT_HOMED` to `not homed`."""
    return member.name.lower().replace("_", " ")


def error_map(*errors: type[Exception]) -> dict[str, type[Exception]]:
    """Map the SiLA identifiers of defined execution errors to the shared exception classes."""
    return {error.__name__: error for error in errors}


def to_wire(value: object) -> object:
    """Convert a shared wire type into the plain value `sila.client` sends."""
    if isinstance(value, enum.Enum):
        return wire_name(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {pascal(field.name): to_wire(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, list):
        return [to_wire(item) for item in value]
    return value


def from_wire[T](tp: type[T], value: object) -> T:
    """
    Convert a plain value received from `sila.client` into a shared wire type, checking its shape.

    Raises:
      ProtocolMismatchError: If the value does not have the expected shape.
    """
    return typing.cast("T", _decode(tp, value))  # the only cast in the client


def list_from_wire[T](item_type: type[T], value: object) -> list[T]:
    """
    Convert a received list into a list of shared wire types, checking every item.

    Raises:
      ProtocolMismatchError: If the value is not a list of the expected items.
    """
    if not isinstance(value, list):
        msg = f"Expected a list of {item_type.__name__}, received {value!r}."
        raise ProtocolMismatchError(msg)
    return [from_wire(item_type, item) for item in value]


def response(responses: object, identifier: str) -> object:
    """
    Pick one named response out of a command's responses.

    Raises:
      ProtocolMismatchError: If the response is missing.
    """
    if not isinstance(responses, collections.abc.Mapping) or identifier not in responses:
        msg = f"Expected a response {identifier!r}, received {responses!r}."
        raise ProtocolMismatchError(msg)
    return responses[identifier]


def _decode(tp: object, value: object) -> object:
    origin = typing.get_origin(tp)
    if origin is list:
        (item_type,) = typing.get_args(tp)
        if not isinstance(value, list):
            msg = f"Expected a list, received {value!r}."
            raise ProtocolMismatchError(msg)
        return [_decode(item_type, item) for item in value]
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return _decode_enum(tp, value)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _decode_structure(tp, value)
    if tp is float and isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if isinstance(tp, type) and type(value) is tp:
        return value
    msg = f"Expected {tp!r}, received {value!r}."
    raise ProtocolMismatchError(msg)


def _decode_enum(tp: type[enum.Enum], value: object) -> enum.Enum:
    for member in tp:
        if wire_name(member) == value:
            return member
    msg = f"{value!r} is not a {tp.__name__}."
    raise ProtocolMismatchError(msg)


def _decode_structure(tp: "type[DataclassInstance]", value: object) -> object:
    if not isinstance(value, collections.abc.Mapping):
        msg = f"Expected a {tp.__name__} structure, received {value!r}."
        raise ProtocolMismatchError(msg)
    hints = typing.get_type_hints(tp)  # strips the Annotated constraints
    try:
        fields = {field.name: _decode(hints[field.name], value[pascal(field.name)]) for field in dataclasses.fields(tp)}
    except KeyError as error:
        msg = f"The {tp.__name__} structure lacks the element {error.args[0]!r}: {value!r}."
        raise ProtocolMismatchError(msg) from None
    return tp(**fields)


@contextlib.contextmanager
def translate_errors(errors: ErrorMap) -> collections.abc.Generator[None]:
    """
    Translate `sila.client` errors into the shared and client exceptions.

    Args:
      errors: The defined execution errors the called endpoint declares, by identifier.
    """
    try:
        yield
    except DefinedExecutionError as error:
        exception_type = errors.get(error.identifier)
        if exception_type is None:
            msg = f"The server raised the undeclared error {error.identifier!r}: {error}"
            raise ProtocolMismatchError(msg) from error
        raise exception_type(str(error)) from error
    except ValidationError as error:
        parameter = (error.parameter or "").rpartition("/")[2]
        raise InvalidParameterError(str(error), parameter) from error
    except UndefinedExecutionError as error:
        raise DeviceError(str(error)) from error
    except CommandExecutionNotAccepted:
        raise  # never executed: safe for the caller to retry
    except (SiLAConnectionError, SiLATimeoutError) as error:
        raise CommandOutcomeUnknownError(str(error)) from error


def ms(seconds: float | None) -> float | None:
    """Convert a timeout in seconds to the milliseconds `sila.client` expects."""
    return None if seconds is None else seconds * 1000


def require_feature(client: Client, identifier: str) -> FeatureNamespace:
    """
    Resolve a feature by its fully qualified identifier.

    Raises:
      IncompatibleServerError: If the server does not offer the feature at this major version.
    """
    try:
        return client.feature(identifier)
    except AttributeError:
        msg = f"The server does not offer the feature {identifier}."
        raise IncompatibleServerError(msg) from None


class Execution[R, I]:
    """
    Handle to a running observable command.

    `detach()` only stops following the execution locally; it does not stop
    anything on the cycler. Use the feature's own commands for that.
    """

    def __init__(
        self,
        raw: CommandExecution,
        decode: collections.abc.Callable[[object], R],
        decode_intermediate: collections.abc.Callable[[object], I] | None,
        errors: ErrorMap,
    ) -> None:
        self._raw = raw
        self._decode = decode
        self._decode_intermediate = decode_intermediate
        self._errors = errors

    @property
    def uuid(self) -> str:
        """The command execution UUID."""
        return self._raw.command_execution_uuid

    @contextlib.asynccontextmanager
    async def intermediate(self) -> collections.abc.AsyncGenerator[collections.abc.AsyncIterator[I]]:
        """Subscribe to the intermediate responses; the subscription closes when the context exits."""
        decode = self._decode_intermediate
        if decode is None:
            msg = "This command has no intermediate responses."
            raise TypeError(msg)
        with translate_errors(self._errors):
            stream = await self._raw.subscribe_intermediate_responses()

        async def values() -> collections.abc.AsyncIterator[I]:
            async for raw in stream:
                yield decode(raw)

        try:
            yield values()
        finally:
            stream.close()

    async def wait(self) -> R:
        """Wait for the execution to finish and return its decoded result."""
        while not self._raw.done:
            stream = await self._raw.subscribe_status()
            try:
                async for _ in stream:
                    pass
            except SiLAConnectionError:
                continue
            finally:
                stream.close()
        with translate_errors(self._errors):
            return self._decode(await self._raw.get_responses())

    def detach(self) -> None:
        """Stop following the execution locally. The command keeps running on the server."""
        self._raw.cancel()


__all__ = [
    "ErrorMap",
    "Execution",
    "error_map",
    "from_wire",
    "list_from_wire",
    "ms",
    "pascal",
    "require_feature",
    "response",
    "to_wire",
    "translate_errors",
    "wire_name",
]
