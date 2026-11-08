"""
BattINFO metadata for Battery Data Format exports, shared by every OmniCycler server.

batterydf 0.2 keeps a table's metadata in a
sidecar file beside it, as BattINFO records: the test, the cell, the cycler,
its channel and the test protocol. This module fills those records from what
a server knows about a run, the same way for every brand, so the sidecar of
every OmniCycler export reads alike.

The records hold only what the server knows for certain. The w3id
identifiers that a BattINFO registry assigns are left out, and so is
anything a person must state, such as the test kind, the creator or the
licence. The protocol keeps aurora-unicycler's units (mA, mAh, V, s), as on
the wire; a C-rate is in `1/h`. The executed aurora-unicycler protocol is
also stored verbatim under `extras["aurora_unicycler_protocol"]`, because BattINFO's
step model cannot hold every detail of it.
"""

import dataclasses
import json
import math
import typing
import warnings

from aurora_unicycler import (
    ConstantCurrent,
    ConstantVoltage,
    CyclingProtocol,
    ImpedanceSpectroscopy,
    Loop,
    OpenCircuitVoltage,
    Step,
    Tag,
    VoltageScan,
)
from bdf import Metadata

from . import __version__
from .backend import ENDED_STATES, CyclerIdentity
from .types import (
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    DataPoint,
    ProtocolFormat,
    RunInformation,
    Sample,
)

Document = dict[str, typing.Any]

TEST_STATUS = {
    ChannelState.RUNNING: "running",
    ChannelState.PAUSED: "running",
    ChannelState.FINISHED: "completed",
    ChannelState.STOPPED: "aborted",
    ChannelState.FAULTED: "aborted",
}
"""The BattINFO test status of each channel state that holds a run."""

C_RATE_UNIT = "1/h"
"""The unit of a C-rate: current divided by nominal capacity."""

CROSSING_LOOPS_NOTE = (
    "The protocol's loops cross each other, which BattINFO's nested step groups cannot describe; "
    "extras.aurora_unicycler_protocol holds the protocol."
)


def build_metadata(
    *,
    identity: CyclerIdentity,
    channel: ChannelInformation,
    run: RunInformation,
    status: ChannelStatus,
    last_point: DataPoint | None,
) -> Metadata:
    """
    Describe one run as batterydf metadata.

    Args:
      identity: The cycler.
      channel: The channel that ran the protocol.
      run: The run.
      status: The channel's status when the export was made.
      last_point: The run's last record, or None without records.

    Returns:
      The BattINFO test, cell, equipment, channel and test-protocol records,
      validated against batterydf's schemas.
    """
    document: Document = {
        "battinfo_test": {"test": _test(identity, run, status, last_point), "provenance": _provenance()},
        "battinfo_cell": {"cell_instance": _cell(run.sample)},
        "battinfo_equipment": {"equipment": _equipment(identity)},
        "battinfo_channel": {"channel": _channel(channel)},
    }
    if run.protocol_format is ProtocolFormat.AURORA_UNICYCLER:
        unicycler = json.loads(run.protocol)
        document["battinfo_test_protocol"] = protocol_record(_parse(unicycler))
        document["extras"] = {"aurora_unicycler_protocol": unicycler}
    else:
        document["battinfo_test_protocol"] = {
            "notes": ["The protocol is in the cycler's native format, which BattINFO records do not describe."]
        }
    return Metadata.model_validate(document)


def protocol_record(protocol: CyclingProtocol) -> Document:
    """
    Describe an aurora-unicycler protocol as a BattINFO test-protocol record.

    Each technique step becomes a BattINFO step whose `step_id` is its
    position in the method list, counting from one, the same number as the
    BDF `Step ID` column. A loop becomes a group of the steps it repeats,
    with `count` the total number of passes, and the tag it loops to as the
    group's tag. Tag steps are left out.

    Returns:
      The record as a document for `bdf.Metadata`.
    """
    record: Document = {
        "record": protocol.record.model_dump(exclude_none=True),
        "safety": protocol.safety.model_dump(exclude_none=True),
    }
    method = _method(protocol)
    if method is None:
        record["notes"] = [CROSSING_LOOPS_NOTE]
    else:
        record["method"] = method
    return record


@dataclasses.dataclass
class _Item:
    """A BattINFO step with the span of the method list it describes, zero-based and inclusive."""

    first: int
    last: int
    step: Document


def _method(protocol: CyclingProtocol) -> list[Document] | None:
    tags = {step.tag: index for index, step in enumerate(protocol.method) if isinstance(step, Tag)}
    items: list[_Item] = []
    for index, step in enumerate(protocol.method):
        if isinstance(step, Tag):
            continue
        if not isinstance(step, Loop):
            items.append(_Item(index, index, _technique(index + 1, step)))
            continue
        target = tags[step.loop_to] if isinstance(step.loop_to, str) else step.loop_to - 1
        if any(item.first < target <= item.last for item in items):
            return None
        group: Document = {
            "step_id": index + 1,
            "mode": "group",
            "count": step.cycle_count,
            "steps": [item.step for item in items if item.first >= target],
        }
        if isinstance(step.loop_to, str):
            group["tags"] = [step.loop_to]
        items = [item for item in items if item.first < target]
        items.append(_Item(target, index, group))
    return [item.step for item in items]


def _technique(step_id: int, step: Step) -> Document:  # noqa: C901, PLR0912  # one branch per technique and limit
    result: Document = {"step_id": step_id}
    setpoints: Document = {}
    termination: list[Document] = []
    match step:
        case OpenCircuitVoltage():
            result |= {"mode": "rest", "direction": "rest"}
        case ConstantCurrent():
            signed = step.rate_C or step.current_mA or 0.0
            charging = signed > 0
            result |= {"mode": "cc", "direction": "charge" if charging else "discharge"}
            if step.rate_C:
                setpoints["c_rate"] = _quantity(abs(step.rate_C), C_RATE_UNIT)
            elif step.current_mA:
                setpoints["current"] = _quantity(abs(step.current_mA), "mA")
            if step.until_voltage_V is not None:
                termination.append(_limit("voltage", "above" if charging else "below", step.until_voltage_V, "V"))
        case ConstantVoltage():
            result |= {"mode": "cv", "direction": "hold"}
            setpoints["voltage"] = _quantity(step.voltage_V, "V")
            if step.until_rate_C is not None:
                termination.append(_limit("c_rate", "below", abs(step.until_rate_C), C_RATE_UNIT))
            if step.until_current_mA is not None:
                termination.append(_limit("current", "below", abs(step.until_current_mA), "mA"))
        case ImpedanceSpectroscopy():
            result |= {"mode": "eis", "direction": "none"}
            setpoints |= {
                "start_frequency": _quantity(step.start_frequency_Hz, "Hz"),
                "end_frequency": _quantity(step.end_frequency_Hz, "Hz"),
                "points_per_decade": _quantity(step.points_per_decade, "1"),
                "measures_per_point": _quantity(step.measures_per_point, "1"),
            }
            if step.amplitude_V is not None:
                setpoints["amplitude"] = _quantity(step.amplitude_V, "V")
            elif step.amplitude_mA is not None:
                setpoints["amplitude"] = _quantity(step.amplitude_mA, "mA")
            if step.drift_correction:
                result["description"] = "With drift correction."
        case VoltageScan():
            result |= {"mode": "scan", "direction": "none"}
            setpoints |= {
                "start_voltage": _quantity(step.start_voltage_V, "V"),
                "end_voltage": _quantity(step.end_voltage_V, "V"),
                "scan_rate": _quantity(step.scan_rate_mV_per_s, "mV/s"),
            }
        case _:
            msg = f"Step {step_id} is not a technique step."
            raise TypeError(msg)
    until_time = getattr(step, "until_time_s", None)
    if until_time is not None:
        termination.append(_limit("duration", "elapsed", until_time, "s"))
    if setpoints:
        result["setpoints"] = setpoints
    if termination:
        result["termination"] = termination
    return result


def _test(
    identity: CyclerIdentity, run: RunInformation, status: ChannelStatus, last_point: DataPoint | None
) -> Document:
    test: Document = {
        "identifier": run.run_id,
        "name": run.sample.name,
        "instrument_name": f"{identity.manufacturer} {identity.model}",
        "started_at": int(run.start_time),
        "status": TEST_STATUS.get(status.state, "other"),
        "conditions": {"nominal_capacity": {"value": run.sample.nominal_capacity, "unit": "Ah"}},
    }
    ended_at = _ended_at(status, last_point)
    if ended_at is not None:
        test["ended_at"] = ended_at
    deviation = _deviation(status, ended_at)
    if deviation is not None:
        test["conformance"] = {"status": "non-conformant", "deviations": [deviation]}
    return test


def _ended_at(status: ChannelStatus, last_point: DataPoint | None) -> int | None:
    if status.state not in ENDED_STATES or last_point is None or not math.isfinite(last_point.unix_time):
        return None
    return int(last_point.unix_time)


def _deviation(status: ChannelStatus, ended_at: int | None) -> Document | None:
    if status.state is ChannelState.FAULTED:
        deviation: Document = {
            "category": "premature_termination",
            "type": "fault",
            "description": status.fault_reason or "The cycler reported a fault without giving a reason.",
        }
    elif status.state is ChannelState.STOPPED:
        deviation = {
            "category": "operator_intervention",
            "type": "stopped",
            "description": "The protocol was stopped before it ended.",
        }
    else:
        return None
    if status.protocol_step:
        deviation["step_index"] = status.protocol_step - 1
    if ended_at is not None:
        deviation["occurred_at"] = ended_at
    return deviation


def _cell(sample: Sample) -> Document:
    cell: Document = {"name": sample.name}
    if sample.identifier:
        cell["serial_number"] = sample.identifier
    return cell


def _equipment(identity: CyclerIdentity) -> Document:
    return {
        "name": f"{identity.manufacturer} {identity.model}",
        "serial_number": identity.serial_number,
        "comment": f"Firmware or vendor software version {identity.firmware_version}.",
    }


def _channel(channel: ChannelInformation) -> Document:
    return {
        "index": channel.channel,
        "label": f"Channel {channel.channel}",
        "property": {
            "voltage_range": {
                "min_value": channel.minimal_voltage,
                "max_value": channel.maximal_voltage,
                "unit": "V",
                "value_basis": "Rated",
            },
            "maximal_current": {"value": channel.maximal_current, "unit": "A", "value_basis": "Rated"},
        },
    }


def _provenance() -> Document:
    return {"workflow_version": f"omnicycler-features {__version__}"}


def _parse(document: Document) -> CyclingProtocol:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CyclingProtocol.model_validate(document)


def _quantity(value: float, unit: str) -> Document:
    return {"value": value, "unit": unit}


def _limit(quantity: str, direction: str, value: float, unit: str) -> Document:
    return {"quantity": quantity, "direction": direction, "value": value, "unit": unit}


__all__ = ["C_RATE_UNIT", "TEST_STATUS", "build_metadata", "protocol_record"]
