import math

import pytest

from omnicycler.features.simulation import CellParameters, ProtocolSimulation
from tests.helpers import make_protocol

LONG = 1e6


def simulate(protocol, **kwargs):
    simulation = ProtocolSimulation(protocol, start_unix_time=1_000.0, **kwargs)
    return simulation, simulation.advance(LONG)


def test_constant_current_charge_stops_at_voltage():
    protocol = make_protocol([{"step": "constant_current", "rate_C": 1, "until_voltage_V": 4.0}])

    simulation, points = simulate(protocol)

    assert simulation.finished
    assert not simulation.faulted
    assert points[-1].voltage == pytest.approx(4.0, abs=0.01)
    assert points[-1].charging_capacity > 0
    assert points[-1].discharging_capacity == 0


def test_discharge_is_negative_current_and_counted_as_discharge():
    protocol = make_protocol([{"step": "constant_current", "rate_C": -1, "until_voltage_V": 3.4}])

    _, points = simulate(protocol)

    assert all(point.current < 0 for point in points[1:])
    assert points[-1].discharging_capacity > 0
    assert points[-1].discharging_energy > 0
    assert points[-1].charging_capacity == 0


def test_constant_voltage_tapers_to_the_current_limit():
    protocol = make_protocol([{"step": "constant_voltage", "voltage_V": 4.1, "until_current_mA": 50}])

    _, points = simulate(protocol)

    assert abs(points[-1].current) <= 0.05
    assert points[-1].voltage == pytest.approx(4.1, abs=1e-6)


def test_loops_repeat_steps_and_count_cycles():
    protocol = make_protocol(
        [
            {"step": "tag", "tag": "cycle"},
            {"step": "constant_current", "rate_C": 0.5, "until_time_s": 60},
            {"step": "constant_current", "rate_C": -0.5, "until_time_s": 60},
            {"step": "loop", "loop_to": "cycle", "cycle_count": 3},
        ]
    )

    simulation, points = simulate(protocol)

    assert simulation.finished
    assert points[-1].step_count == 6
    assert points[-1].cycle_count == 3
    assert points[-1].test_time == pytest.approx(360)
    assert {point.protocol_step for point in points} == {2, 3}


def test_safety_limit_faults_the_run():
    protocol = make_protocol(
        [{"step": "constant_current", "rate_C": 1, "until_voltage_V": 4.1}],
        safety={"max_voltage_V": 3.8},
    )

    simulation, points = simulate(protocol)

    assert simulation.faulted
    assert not simulation.finished
    assert "maximal voltage" in simulation.fault_reason
    assert points[-1].voltage > 3.8


def test_voltage_scan_follows_the_scan_rate():
    protocol = make_protocol(
        [{"step": "voltage_scan", "start_voltage_V": 3.6, "end_voltage_V": 3.7, "scan_rate_mV_per_s": 1}]
    )

    simulation, points = simulate(protocol)

    assert simulation.finished
    assert points[-1].test_time == pytest.approx(100)
    assert points[-1].voltage == pytest.approx(3.7, abs=0.01)


def test_records_follow_the_record_interval():
    protocol = make_protocol([{"step": "open_circuit_voltage", "until_time_s": 100}])

    _, points = simulate(protocol)

    assert [point.record_index for point in points] == list(range(len(points)))
    assert [point.test_time for point in points] == pytest.approx([10.0 * index for index in range(11)])
    assert all(point.unix_time == pytest.approx(1_000.0 + point.test_time) for point in points)


def test_unmeasured_quantities_are_nan():
    protocol = make_protocol([{"step": "open_circuit_voltage", "until_time_s": 10}])

    _, points = simulate(protocol)

    assert points[0].ambient_temperature == 25.0
    assert math.isnan(points[0].temperature_t2)
    assert math.isnan(points[0].internal_resistance)


def test_advancing_in_pieces_matches_advancing_at_once():
    method = [
        {"step": "constant_current", "rate_C": 1, "until_voltage_V": 4.0},
        {"step": "constant_voltage", "voltage_V": 4.0, "until_rate_C": 0.1},
    ]
    whole, pieces = (ProtocolSimulation(make_protocol(method), start_unix_time=0.0) for _ in range(2))

    expected = whole.advance(LONG)
    produced = [point for _ in range(2000) for point in pieces.advance(7.3)]

    assert [point.record_index for point in produced] == [point.record_index for point in expected]
    assert [point.test_time for point in produced] == pytest.approx([point.test_time for point in expected])
    assert [point.voltage for point in produced] == pytest.approx([point.voltage for point in expected], abs=1e-6)


def test_current_is_clamped_to_the_channel_limit():
    protocol = make_protocol([{"step": "constant_voltage", "voltage_V": 4.2, "until_time_s": 10}])

    simulation = ProtocolSimulation(
        protocol, start_unix_time=0.0, cell=CellParameters(nominal_capacity=1.0), maximal_current=0.5
    )
    points = simulation.advance(10)

    assert max(point.current for point in points) == pytest.approx(0.5)


def test_protocol_without_capacity_is_refused():
    merged = make_protocol([{"step": "open_circuit_voltage", "until_time_s": 10}])
    protocol = merged.model_copy(update={"sample": merged.sample.model_copy(update={"capacity_mAh": None})})

    with pytest.raises(ValueError, match="capacity"):
        ProtocolSimulation(protocol, start_unix_time=0.0)
