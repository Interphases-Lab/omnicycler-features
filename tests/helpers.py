import collections.abc
import json

from aurora_unicycler import CyclingProtocol

from omnicycler.features.protocol import parse_protocol
from omnicycler.features.types import Sample


def make_protocol(
    method: collections.abc.Sequence[collections.abc.Mapping[str, object]], *, capacity: float = 1.0, **extra: object
) -> CyclingProtocol:
    """Build a merged, validated aurora-unicycler protocol, by default for a 1 Ah cell recorded every 10 s."""
    document = {"record": {"time_s": 10}, "method": list(method), **extra}
    return parse_protocol(json.dumps(document).encode(), Sample(name="test-cell", nominal_capacity=capacity)).protocol
