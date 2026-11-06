"""
Aurora-unicycler protocol handling, shared by every OmniCycler server.

Protocols travel over SiLA as aurora-unicycler JSON bytes. This module
decodes them, merges the `Sample` parameter into them and validates them
with aurora-unicycler itself, so every brand server accepts and rejects
exactly the same protocols.
"""

import dataclasses
import json
import warnings

import aurora_unicycler
import pydantic
from aurora_unicycler import (
    ConstantCurrent,
    ConstantVoltage,
    CyclingProtocol,
    ImpedanceSpectroscopy,
    OpenCircuitVoltage,
    ProtocolMethodWarning,
    Step,
    VoltageScan,
)
from unitelabs.cdk import sila

from .errors import InvalidProtocolError, InvalidSampleError
from .types import Sample, StepType

CONTENT_TYPE = sila.constraints.ContentType("application", "json")
"""The content type of a protocol on the wire."""

PLACEHOLDER_SAMPLE_NAME = "$NAME"
"""Aurora-unicycler's placeholder for a sample name that has not been set."""

_CAPACITY_TOLERANCE = 1e-9
"""Capacities in mAh closer than this are the same."""

_STEP_TYPES: dict[type[Step], StepType] = {
    OpenCircuitVoltage: StepType.OPEN_CIRCUIT_VOLTAGE,
    ConstantCurrent: StepType.CONSTANT_CURRENT,
    ConstantVoltage: StepType.CONSTANT_VOLTAGE,
    ImpedanceSpectroscopy: StepType.IMPEDANCE_SPECTROSCOPY,
    VoltageScan: StepType.VOLTAGE_SCAN,
}


@dataclasses.dataclass(frozen=True)
class ParsedProtocol:
    """
    A protocol decoded from the wire, with the sample merged in.

    Attributes:
      protocol: The validated aurora-unicycler protocol.
      warnings: Warnings aurora-unicycler or the sample merge raised.
    """

    protocol: CyclingProtocol
    warnings: list[str]


def unicycler_version() -> str:
    """Return the aurora-unicycler version this package validates protocols with."""
    return aurora_unicycler.__version__


def parse_protocol(data: bytes, sample: Sample) -> ParsedProtocol:
    """
    Decode protocol bytes, merge the sample into them and validate the result.

    The sample always replaces the protocol's own `sample` block, so one
    protocol file can serve as a template for many cells. Nominal capacity is
    converted from Ah to aurora-unicycler's mAh here, and nowhere else.

    Args:
      data: UTF-8 encoded aurora-unicycler JSON.
      sample: The cell under test.

    Returns:
      The validated protocol and any warnings.

    Raises:
      InvalidSampleError: If the sample name is aurora-unicycler's placeholder.
      InvalidProtocolError: If the bytes are not a valid aurora-unicycler protocol.
    """
    if sample.name.strip() in {"", PLACEHOLDER_SAMPLE_NAME}:
        msg = f"Sample name {sample.name!r} is a placeholder; give the cell a real name."
        raise InvalidSampleError(msg)

    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        msg = f"Protocol is not UTF-8 encoded JSON: {error}"
        raise InvalidProtocolError(msg) from None

    if not isinstance(document, dict):
        msg = f"Protocol must be a JSON object, received {type(document).__name__}."
        raise InvalidProtocolError(msg)

    messages = _sample_replacement_warnings(document.get("sample"), sample)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ProtocolMethodWarning)
        try:
            protocol = CyclingProtocol.from_dict(
                document, sample_name=sample.name, sample_capacity_mAh=sample.nominal_capacity * 1000
            )
        except (pydantic.ValidationError, TypeError, ValueError) as error:
            msg = f"Protocol is not a valid aurora-unicycler protocol: {error}"
            raise InvalidProtocolError(msg) from None

    messages.extend(str(warning.message) for warning in caught if issubclass(warning.category, UserWarning))
    return ParsedProtocol(protocol=protocol, warnings=messages)


def encode_protocol(protocol: CyclingProtocol) -> bytes:
    """Encode a protocol as aurora-unicycler JSON bytes."""
    return protocol.model_dump_json().encode("utf-8")


def step_type(step: Step) -> StepType | None:
    """Return the technique type of a step, or `None` for loop and tag steps."""
    return _STEP_TYPES.get(type(step))


def technique_steps(protocol: CyclingProtocol) -> list[tuple[int, Step, StepType]]:
    """
    List the technique steps of a protocol.

    Returns:
      Each technique step with its position in the method list, counting
      from one, and its type. Loop and tag steps are left out.
    """
    return [
        (position, step, kind)
        for position, step in enumerate(protocol.method, start=1)
        if (kind := step_type(step)) is not None
    ]


def step_current(step: ConstantCurrent, nominal_capacity: float) -> float:
    """
    Return the current of a constant-current step in ampere.

    Args:
      step: The constant-current step.
      nominal_capacity: The nominal capacity of the cell in ampere-hours.

    Returns:
      The current, positive when charging. A C-rate takes priority over an
      absolute current, as in aurora-unicycler.
    """
    if step.rate_C:
        return step.rate_C * nominal_capacity
    return (step.current_mA or 0.0) / 1000


def _sample_replacement_warnings(block: object, sample: Sample) -> list[str]:
    if not isinstance(block, dict):
        return []

    name = block.get("name")
    capacity = block.get("capacity_mAh")
    replaced = []
    if name not in {None, "", PLACEHOLDER_SAMPLE_NAME} and name != sample.name:
        replaced.append(f"name {name!r}")
    if isinstance(capacity, int | float) and abs(capacity - sample.nominal_capacity * 1000) > _CAPACITY_TOLERANCE:
        replaced.append(f"capacity {capacity} mAh")

    if not replaced:
        return []
    return [f"The protocol's sample block ({', '.join(replaced)}) was replaced by the Sample parameter."]


__all__ = [
    "CONTENT_TYPE",
    "PLACEHOLDER_SAMPLE_NAME",
    "ParsedProtocol",
    "encode_protocol",
    "parse_protocol",
    "step_current",
    "step_type",
    "technique_steps",
    "unicycler_version",
]
