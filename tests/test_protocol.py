import collections.abc
import json

import pytest
from aurora_unicycler import ConstantCurrent

from omnicycler.features.errors import InvalidProtocolError, InvalidSampleError
from omnicycler.features.protocol import (
    encode_protocol,
    parse_protocol,
    step_current,
    technique_steps,
    unicycler_version,
)
from omnicycler.features.types import Sample, StepType

SAMPLE = Sample(name="cell-7", nominal_capacity=0.0045)
METHOD = [
    {"step": "tag", "tag": "cycle"},
    {"step": "constant_current", "rate_C": 0.5, "until_voltage_V": 4.2},
    {"step": "constant_voltage", "voltage_V": 4.2, "until_rate_C": 0.05},
    {"step": "constant_current", "rate_C": -0.5, "until_voltage_V": 3.0},
    {"step": "loop", "loop_to": "cycle", "cycle_count": 3},
]


SAFETY = {"min_voltage_V": 2.5, "max_voltage_V": 4.3}


def encode(document: collections.abc.Mapping[str, object], *, safety: bool = True) -> bytes:
    """Encode a protocol document, with voltage limits unless `safety` is false."""
    return json.dumps({"safety": SAFETY, **document} if safety else document).encode()


def test_sample_is_merged_with_capacity_in_mah():
    parsed = parse_protocol(encode({"record": {"time_s": 10}, "method": METHOD}), SAMPLE)

    assert parsed.protocol.sample.name == "cell-7"
    assert parsed.protocol.sample.capacity_mAh == pytest.approx(4.5)
    assert parsed.warnings == []


def test_replaced_sample_block_is_reported():
    document = {"sample": {"name": "other", "capacity_mAh": 3}, "record": {"time_s": 10}, "method": METHOD}

    parsed = parse_protocol(encode(document), SAMPLE)

    assert parsed.protocol.sample.name == "cell-7"
    assert any("replaced by the Sample parameter" in warning for warning in parsed.warnings)


def test_placeholder_sample_block_is_not_reported():
    document = {"sample": {"name": "$NAME"}, "record": {"time_s": 10}, "method": METHOD}

    assert parse_protocol(encode(document), SAMPLE).warnings == []


def test_unicycler_warnings_are_collected():
    parsed = parse_protocol(encode({"method": METHOD}), SAMPLE)

    assert any("record" in warning for warning in parsed.warnings)


@pytest.mark.parametrize("name", ["$NAME", " "])
def test_placeholder_sample_name_is_rejected(name: str):
    with pytest.raises(InvalidSampleError):
        parse_protocol(encode({"method": METHOD}), Sample(name=name, nominal_capacity=1.0))


@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xfe",
        b"not json",
        b"[1, 2]",
        encode({"method": []}),
        encode({"method": [{"step": "teleport"}]}),
        encode({"method": METHOD, "unknown_field": 1}),
    ],
)
def test_invalid_protocols_are_rejected(data: bytes):
    with pytest.raises(InvalidProtocolError):
        parse_protocol(data, SAMPLE)


@pytest.mark.parametrize(
    ("safety", "missing"),
    [
        (None, "safety.min_voltage_V and no safety.max_voltage_V"),
        ({"max_voltage_V": 4.3}, "safety.min_voltage_V"),
        ({"min_voltage_V": 2.5, "max_current_mA": 100}, "safety.max_voltage_V"),
    ],
)
def test_protocol_without_voltage_limits_is_rejected(safety: dict[str, float] | None, missing: str):
    document: dict[str, object] = {"record": {"time_s": 10}, "method": METHOD}
    if safety is not None:
        document["safety"] = safety

    with pytest.raises(InvalidProtocolError, match=f"sets no {missing}"):
        parse_protocol(encode(document, safety=False), SAMPLE)


def test_technique_steps_skip_tags_and_loops():
    protocol = parse_protocol(encode({"method": METHOD}), SAMPLE).protocol

    steps = [(position, kind) for position, _, kind in technique_steps(protocol)]

    assert steps == [(2, StepType.CONSTANT_CURRENT), (3, StepType.CONSTANT_VOLTAGE), (4, StepType.CONSTANT_CURRENT)]


def test_step_current_prefers_c_rate():
    assert step_current(ConstantCurrent(rate_C=-0.5, until_time_s=1), 2.0) == pytest.approx(-1.0)
    assert step_current(ConstantCurrent(current_mA=250, until_time_s=1), 2.0) == pytest.approx(0.25)


def test_encoded_protocol_parses_back_unchanged():
    protocol = parse_protocol(encode({"record": {"time_s": 10}, "method": METHOD}), SAMPLE).protocol

    again = parse_protocol(encode_protocol(protocol), SAMPLE).protocol

    assert again == protocol


def test_unicycler_version_is_reported():
    assert unicycler_version().startswith("0.5")
