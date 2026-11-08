"""SiLA parameter types shared by the standard features."""

import collections.abc
import typing

from unitelabs.cdk import sila

from .protocol import CONTENT_TYPE
from .types import RunId

ChannelNumber = typing.Annotated[int, sila.constraints.MinimalInclusive(1)]
"""A channel number. Each feature narrows it to the cycler's channels at startup with `restrict_channels`."""

RunIdentifier = RunId
"""The run a command acts on or reads; see `types.RunId`."""

ProtocolDocument = typing.Annotated[bytes, CONTENT_TYPE]
"""An aurora-unicycler protocol as UTF-8 encoded JSON."""

JsonDocument = typing.Annotated[bytes, CONTENT_TYPE]
"""A UTF-8 encoded JSON document."""


def restrict_channels(
    feature: sila.Feature,
    handlers: collections.abc.Iterable[collections.abc.Callable[..., typing.Any]],
    channels: collections.abc.Sequence[int],
) -> None:
    """
    Constrain the `channel` parameter of each handler to the given channel numbers.

    Args:
      feature: The feature the handlers belong to.
      handlers: The bound command methods taking a `channel` parameter.
      channels: The channel numbers of the cycler.
    """
    for handler in handlers:
        feature.add_constraint(handler, "channel", [sila.constraints.Set(list(channels))])


__all__ = ["ChannelNumber", "JsonDocument", "ProtocolDocument", "RunIdentifier", "restrict_channels"]
