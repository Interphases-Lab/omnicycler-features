"""Default protocols and samples the conformance suite runs; brand repos may override the fixtures."""

import json

from ..types import ChannelInformation, Sample


def short_protocol() -> bytes:
    """A protocol that finishes within minutes of simulated time: charge, rest, discharge."""
    return _encode(
        [
            {"step": "constant_current", "rate_C": 0.5, "until_voltage_V": 4.0, "until_time_s": 600},
            {"step": "open_circuit_voltage", "until_time_s": 60},
            {"step": "constant_current", "rate_C": -0.5, "until_voltage_V": 3.5, "until_time_s": 600},
        ]
    )


def long_protocol() -> bytes:
    """A protocol that keeps a channel running for the whole test: a rest of about four months."""
    return _encode([{"step": "open_circuit_voltage", "until_time_s": 1e7}])


def faulting_protocol(sample: Sample) -> bytes:
    """A protocol that trips its safety limit: it charges for an hour but allows 1 % of the sample's capacity."""
    document = {
        "record": {"time_s": 10},
        "safety": {"max_capacity_mAh": sample.nominal_capacity * 1000 * 0.01},
        "method": [{"step": "constant_current", "rate_C": 0.5, "until_time_s": 3600}],
    }
    return json.dumps(document).encode("utf-8")


def sample_for(channel: ChannelInformation) -> Sample:
    """A sample whose 1C current is half the channel's maximal current."""
    return Sample(name="conformance-cell", nominal_capacity=channel.maximal_current / 2)


def _encode(method: list[dict[str, object]]) -> bytes:
    return json.dumps({"record": {"time_s": 10}, "method": method}).encode("utf-8")


__all__ = ["faulting_protocol", "long_protocol", "sample_for", "short_protocol"]
