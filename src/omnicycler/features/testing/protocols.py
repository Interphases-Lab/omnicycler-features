"""Default protocols and samples the conformance suite runs; brand repos may override the fixtures."""

import json

from ..types import ChannelInformation, Sample

SAFETY = {"min_voltage_V": 2.5, "max_voltage_V": 4.3}
"""The voltage safety limits of every default protocol, which OmniCycler requires; within a 0 V to 5 V channel."""

CHARGE_RATE = 0.5
"""The C-rate of the short protocol's charge (step 1) and, negated, its discharge (step 3)."""


def short_protocol() -> bytes:
    """A protocol that finishes within minutes of simulated time: charge, rest, discharge."""
    return _encode(
        [
            {"step": "constant_current", "rate_C": CHARGE_RATE, "until_voltage_V": 4.0, "until_time_s": 600},
            {"step": "open_circuit_voltage", "until_time_s": 60},
            {"step": "constant_current", "rate_C": -CHARGE_RATE, "until_voltage_V": 3.5, "until_time_s": 600},
        ]
    )


def long_protocol() -> bytes:
    """A protocol that keeps a channel running for the whole test: a rest of about four months."""
    return _encode([{"step": "open_circuit_voltage", "until_time_s": 1e7}])


def faulting_protocol(sample: Sample) -> bytes:
    """A protocol that trips its safety limit: it charges for an hour but allows 1 % of the sample's capacity."""
    document = {
        "record": {"time_s": 10},
        "safety": {**SAFETY, "max_capacity_mAh": sample.nominal_capacity * 1000 * 0.01},
        "method": [{"step": "constant_current", "rate_C": 0.5, "until_time_s": 3600}],
    }
    return json.dumps(document).encode("utf-8")


def sample_for(channel: ChannelInformation) -> Sample:
    """A sample whose 1C current is half the channel's maximal current."""
    return Sample(name="conformance-cell", nominal_capacity=channel.maximal_current / 2, identifier="CONF-0001")


def _encode(method: list[dict[str, object]]) -> bytes:
    return json.dumps({"record": {"time_s": 10}, "safety": SAFETY, "method": method}).encode("utf-8")


__all__ = ["CHARGE_RATE", "SAFETY", "faulting_protocol", "long_protocol", "sample_for", "short_protocol"]
