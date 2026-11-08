import bdf
import pytest

from omnicycler.features.backend import CyclerIdentity
from omnicycler.features.bdf_export import encode_bdf, to_frame
from omnicycler.features.bdf_metadata import CROSSING_LOOPS_NOTE, build_metadata, protocol_record
from omnicycler.features.protocol import encode_protocol
from omnicycler.features.simulation import ProtocolSimulation
from omnicycler.features.types import (
    BdfFormat,
    ChannelInformation,
    ChannelState,
    ChannelStatus,
    ProtocolFormat,
    RunInformation,
    Sample,
)
from tests.helpers import make_protocol

START = 1_700_000_000.0
IDENTITY = CyclerIdentity(manufacturer="Acme", model="Cycler 9", serial_number="SN-42", firmware_version="1.2.3")
CHANNEL = ChannelInformation(
    channel=3, minimal_voltage=0.0, maximal_voltage=5.0, maximal_current=2.0, measured_quantities=[]
)
LOOPED = [
    {"step": "tag", "tag": "cycle"},
    {"step": "constant_current", "rate_C": 1, "until_time_s": 120},
    {"step": "constant_current", "rate_C": -1, "until_time_s": 60},
    {"step": "loop", "loop_to": "cycle", "cycle_count": 2},
]


@pytest.fixture
def protocol():
    return make_protocol(LOOPED, safety={"max_voltage_V": 4.3})


@pytest.fixture
def points(protocol):
    return ProtocolSimulation(protocol, start_unix_time=START).advance(1e6)


def run_information(protocol, protocol_format=ProtocolFormat.AURORA_UNICYCLER, data=None):
    return RunInformation(
        run_id="run-1",
        sample=Sample(name="test-cell", nominal_capacity=1.0),
        protocol_format=protocol_format,
        protocol=encode_protocol(protocol) if data is None else data,
        start_time=START,
    )


def metadata(protocol, points, state, *, run=None, step=2, **status):
    channel_status = ChannelStatus(
        channel=3,
        state=state,
        run_id="run-1",
        voltage=3.7,
        current=0.0,
        test_time=points[-1].test_time,
        cycle_count=1,
        protocol_step=step,
        record_count=len(points),
        **status,
    )
    return build_metadata(
        identity=IDENTITY,
        channel=CHANNEL,
        run=run or run_information(protocol),
        status=channel_status,
        last_point=points[-1] if points else None,
    )


def test_finished_run_is_described(protocol, points):
    meta = metadata(protocol, points, ChannelState.FINISHED)

    test = meta.battinfo_test.test
    assert test.identifier == "run-1"
    assert test.name == "test-cell"
    assert test.status == "completed"
    assert test.started_at == int(START)
    assert test.ended_at == int(points[-1].unix_time)
    assert test.instrument_name == "Acme Cycler 9"
    assert test.conditions["nominal_capacity"].value == 1.0
    assert test.conformance.deviations is None
    assert meta.battinfo_cell.cell_instance.name == "test-cell"
    assert meta.battinfo_equipment.equipment.serial_number == "SN-42"
    assert meta.battinfo_channel.channel.index == 3
    assert meta.battinfo_channel.channel.property["voltage_range"].max_value == 5.0
    assert meta.battinfo_test_protocol.safety.max_voltage_V == 4.3
    assert meta.battinfo_test_protocol.record.time_s == 10
    assert meta.extras["aurora_unicycler_protocol"]["method"][1]["rate_C"] == 1


def test_running_run_has_no_end(protocol, points):
    test = metadata(protocol, points, ChannelState.RUNNING).battinfo_test.test

    assert test.status == "running"
    assert test.ended_at is None


def test_fault_is_a_deviation_with_its_reason(protocol, points):
    test = metadata(protocol, points, ChannelState.FAULTED, step=3, fault_reason="Over-temperature").battinfo_test.test

    assert test.status == "aborted"
    assert test.conformance.status == "non-conformant"
    (deviation,) = test.conformance.deviations
    assert deviation.category == "premature_termination"
    assert deviation.description == "Over-temperature"
    assert deviation.step_index == 2
    assert deviation.occurred_at == test.ended_at


def test_fault_without_reason_says_so(protocol, points):
    test = metadata(protocol, points, ChannelState.FAULTED).battinfo_test.test

    assert "without giving a reason" in test.conformance.deviations[0].description


def test_stop_is_an_operator_intervention(protocol, points):
    test = metadata(protocol, points, ChannelState.STOPPED).battinfo_test.test

    assert test.status == "aborted"
    assert test.conformance.deviations[0].category == "operator_intervention"


def test_steps_and_nested_loops():
    protocol = make_protocol(
        [
            {"step": "open_circuit_voltage", "until_time_s": 60},
            {"step": "tag", "tag": "outer"},
            {"step": "constant_current", "current_mA": -500, "until_voltage_V": 3.0},
            {"step": "tag", "tag": "inner"},
            {"step": "constant_voltage", "voltage_V": 4.1, "until_current_mA": 50, "until_rate_C": 0.05},
            {"step": "open_circuit_voltage", "until_time_s": 10},
            {"step": "loop", "loop_to": "inner", "cycle_count": 3},
            {"step": "loop", "loop_to": "outer", "cycle_count": 2},
        ]
    )

    rest = {"mode": "rest", "direction": "rest"}
    assert protocol_record(protocol)["method"] == [
        {"step_id": 1, **rest, "termination": [_limit("duration", "elapsed", 60, "s")]},
        {
            "step_id": 8,
            "mode": "group",
            "count": 2,
            "tags": ["outer"],
            "steps": [
                {
                    "step_id": 3,
                    "mode": "cc",
                    "direction": "discharge",
                    "setpoints": {"current": {"value": 500, "unit": "mA"}},
                    "termination": [_limit("voltage", "below", 3.0, "V")],
                },
                {
                    "step_id": 7,
                    "mode": "group",
                    "count": 3,
                    "tags": ["inner"],
                    "steps": [
                        {
                            "step_id": 5,
                            "mode": "cv",
                            "direction": "hold",
                            "setpoints": {"voltage": {"value": 4.1, "unit": "V"}},
                            "termination": [
                                _limit("c_rate", "below", 0.05, "1/h"),
                                _limit("current", "below", 50, "mA"),
                            ],
                        },
                        {"step_id": 6, **rest, "termination": [_limit("duration", "elapsed", 10, "s")]},
                    ],
                },
            ],
        },
    ]


def test_loop_to_a_step_number_has_no_tag():
    protocol = make_protocol([*LOOPED[1:3], {"step": "loop", "loop_to": 1, "cycle_count": 2}])

    (group,) = protocol_record(protocol)["method"]

    assert group["count"] == 2
    assert "tags" not in group
    assert [step["step_id"] for step in group["steps"]] == [1, 2]


def test_crossing_loops_leave_the_method_out():
    protocol = make_protocol(
        [
            {"step": "tag", "tag": "a"},
            {"step": "constant_current", "rate_C": 1, "until_time_s": 60},
            {"step": "tag", "tag": "b"},
            {"step": "open_circuit_voltage", "until_time_s": 60},
            {"step": "loop", "loop_to": "a", "cycle_count": 2},
            {"step": "open_circuit_voltage", "until_time_s": 60},
            {"step": "loop", "loop_to": "b", "cycle_count": 2},
        ]
    )

    record = protocol_record(protocol)

    assert "method" not in record
    assert record["notes"] == [CROSSING_LOOPS_NOTE]


def test_impedance_and_voltage_scan_setpoints():
    protocol = make_protocol(
        [
            {
                "step": "impedance_spectroscopy",
                "amplitude_V": 0.01,
                "start_frequency_Hz": 1e4,
                "end_frequency_Hz": 1,
                "drift_correction": True,
            },
            {"step": "voltage_scan", "start_voltage_V": 3.6, "end_voltage_V": 3.8, "scan_rate_mV_per_s": 1},
        ]
    )

    impedance, scan = protocol_record(protocol)["method"]

    assert impedance["mode"] == "eis"
    assert impedance["setpoints"]["amplitude"] == {"value": 0.01, "unit": "V"}
    assert impedance["setpoints"]["start_frequency"] == {"value": 1e4, "unit": "Hz"}
    assert impedance["description"] == "With drift correction."
    assert scan["mode"] == "scan"
    assert scan["setpoints"]["scan_rate"] == {"value": 1, "unit": "mV/s"}


def test_native_protocol_is_only_noted(protocol, points):
    run = run_information(protocol, ProtocolFormat.NATIVE, b"\x00vendor")

    meta = metadata(protocol, points, ChannelState.FINISHED, run=run)

    assert meta.battinfo_test_protocol.method is None
    assert "native format" in meta.battinfo_test_protocol.notes[0]
    assert meta.extras is None


def test_step_ids_match_the_step_id_column(protocol, points):
    meta = metadata(protocol, points, ChannelState.FINISHED)

    described = set(_step_ids(meta.battinfo_test_protocol.method))

    assert set(to_frame(points)["Step ID"].to_list()) <= described


@pytest.mark.parametrize("data_format", list(BdfFormat))
def test_batterydf_reads_the_export_back(protocol, points, tmp_path, data_format):
    export = encode_bdf(points, data_format, metadata(protocol, points, ChannelState.FINISHED))

    table, sidecar = export.save(tmp_path / f"test-cell.bdf.{data_format.name.lower()}")
    frame, meta = bdf.read(table)

    assert sidecar.name == "test-cell.bdf.metadata.json"
    assert len(frame) == len(points)
    test, method = meta.battinfo_test.test, meta.battinfo_test_protocol.method
    assert test is not None
    assert test.identifier == "run-1"
    assert method is not None
    assert method[0].count == 2
    assert meta.bdf.bdf_version


def _limit(quantity, direction, value, unit):
    return {"quantity": quantity, "direction": direction, "value": value, "unit": unit}


def _step_ids(steps):
    for step in steps or []:
        if step.mode == "group":
            yield from _step_ids(step.steps)
        else:
            yield step.step_id
