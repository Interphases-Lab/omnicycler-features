import collections.abc
import json

from aurora_unicycler import CyclingProtocol

from omnicycler.features.protocol import parse_protocol
from omnicycler.features.types import Sample

SAFETY = {"min_voltage_V": 0.0, "max_voltage_V": 5.0}
"""Voltage limits every protocol needs; the reference simulator's channel range, so they never trip by default."""


def protocol_document(
    method: collections.abc.Sequence[collections.abc.Mapping[str, object]], **extra: object
) -> dict[str, object]:
    """An aurora-unicycler document recorded every 10 s, with `SAFETY` merged under any `safety` given."""
    safety = {**SAFETY, **_as_dict(extra.pop("safety", {}))}
    return {"record": {"time_s": 10}, "safety": safety, "method": list(method), **extra}


def make_protocol(
    method: collections.abc.Sequence[collections.abc.Mapping[str, object]], *, capacity: float = 1.0, **extra: object
) -> CyclingProtocol:
    """Build a merged, validated aurora-unicycler protocol, by default for a 1 Ah cell recorded every 10 s."""
    document = protocol_document(method, **extra)
    return parse_protocol(json.dumps(document).encode(), Sample(name="test-cell", nominal_capacity=capacity)).protocol


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value
