"""A simple equivalent-circuit cell that executes aurora-unicycler protocols in simulated time."""

import dataclasses
import math

from aurora_unicycler import (
    ConstantCurrent,
    ConstantVoltage,
    CyclingProtocol,
    Loop,
    OpenCircuitVoltage,
    Step,
    Tag,
    VoltageScan,
)

from ..protocol import step_current
from ..types import DataPoint, MeasuredQuantity

TIME_STEP = 1.0
"""The integration time step in seconds of simulated time."""

DEFAULT_RECORD_INTERVAL = 10.0
"""The record interval in seconds when a protocol sets none."""

MEASURED_QUANTITIES = (MeasuredQuantity.AMBIENT_TEMPERATURE, MeasuredQuantity.SURFACE_TEMPERATURE)
"""The optional quantities the simulated cell reports."""


@dataclasses.dataclass(frozen=True)
class CellParameters:
    """
    The parameters of a simulated cell.

    The open-circuit voltage rises linearly from `empty_voltage` to
    `full_voltage` with the state of charge, and a series resistance adds
    the overpotential. The surface temperature rises with the C-rate.

    Attributes:
      nominal_capacity: The capacity in ampere-hours.
      resistance: The series resistance in ohm.
      empty_voltage: The open-circuit voltage when empty, in volt.
      full_voltage: The open-circuit voltage when full, in volt.
      initial_state_of_charge: The state of charge at the start, from 0 to 1.
      ambient_temperature: The ambient temperature in degrees Celsius.
      heating: The surface temperature rise per C-rate, in kelvin.
    """

    nominal_capacity: float
    resistance: float = 0.05
    empty_voltage: float = 3.0
    full_voltage: float = 4.2
    initial_state_of_charge: float = 0.5
    ambient_temperature: float = 25.0
    heating: float = 2.0


class ProtocolSimulation:
    """
    Executes an aurora-unicycler protocol on a simulated cell.

    Call `advance` with an amount of simulated time; it returns the data
    records produced in that time. The simulation ends when the protocol
    ends (`finished`) or a safety limit is exceeded (`faulted`).
    """

    def __init__(
        self,
        protocol: CyclingProtocol,
        *,
        start_unix_time: float,
        cell: CellParameters | None = None,
        maximal_current: float = math.inf,
    ) -> None:
        capacity = (protocol.sample.capacity_mAh or 0.0) / 1000
        if capacity <= 0:
            msg = "The protocol's sample has no capacity; merge a Sample into it first."
            raise ValueError(msg)

        self.finished = False
        self.faulted = False
        self.fault_reason = ""

        self._protocol = protocol
        self._steps: list[Step] = list(protocol.method)
        self._tags = {step.tag: index for index, step in enumerate(self._steps) if isinstance(step, Tag)}
        self._cell = cell or CellParameters(nominal_capacity=capacity)
        self._maximal_current = maximal_current
        self._start_unix_time = start_unix_time

        self._state_of_charge = self._cell.initial_state_of_charge
        self._position = 0
        self._loops_done: dict[int, int] = {}
        self._test_time = 0.0
        self._step_time = 0.0
        self._cycle_count = 1
        self._step_count = 0
        self._charging_capacity = 0.0
        self._discharging_capacity = 0.0
        self._charging_energy = 0.0
        self._discharging_energy = 0.0
        self._voltage = self._open_circuit_voltage()
        self._current = 0.0
        self._record_index = 0
        self._next_record_time = 0.0
        self._pending = 0.0

        self._enter_step()

    @property
    def done(self) -> bool:
        """Whether the protocol has ended, normally or by a fault."""
        return self.finished or self.faulted

    @property
    def voltage(self) -> float:
        """The latest cell voltage in volt."""
        return self._voltage

    @property
    def current(self) -> float:
        """The latest current in ampere, positive when charging."""
        return self._current

    @property
    def test_time(self) -> float:
        """The simulated time since the start in seconds."""
        return self._test_time

    @property
    def cycle_count(self) -> int:
        """The current cycle, counting from one."""
        return self._cycle_count

    @property
    def protocol_step(self) -> int:
        """The position of the executing step in the method list, counting from one, or zero when done."""
        return 0 if self.done else self._position + 1

    def advance(self, duration: float) -> list[DataPoint]:
        """
        Run the protocol for an amount of simulated time.

        Args:
          duration: The simulated time in seconds.

        Returns:
          The data records produced, in order.
        """
        points: list[DataPoint] = []
        if not self.done and self._record_index == 0:
            points.append(self._record())

        # Only whole integration steps are taken; leftover time carries over to the next call, so the result does
        # not depend on how the simulated time is split into calls.
        self._pending += duration
        while not self.done:
            step = self._steps[self._position]
            dt = min(TIME_STEP, self._time_to_step_limit(step))
            if dt > self._pending + 1e-9:
                break
            self._integrate(step, dt)
            self._pending = max(self._pending - dt, 0.0)

            ended = self._check_safety() or self._step_ended(step)
            if ended or self._test_time >= self._next_record_time:
                points.append(self._record())
            if ended and not self.faulted:
                self._position += 1
                self._enter_step()
        return points

    # Protocol flow.

    def _enter_step(self) -> None:
        for _ in range(100_000):
            if self._position >= len(self._steps):
                self.finished = True
                return
            step = self._steps[self._position]
            if isinstance(step, Tag):
                self._position += 1
            elif isinstance(step, Loop):
                self._follow_loop(step)
            else:
                self._step_count += 1
                self._step_time = 0.0
                return
        msg = "The protocol loops without executing any step."
        raise RuntimeError(msg)

    def _follow_loop(self, step: Loop) -> None:
        done = self._loops_done.get(self._position, 0) + 1
        if done < step.cycle_count:
            self._loops_done[self._position] = done
            self._cycle_count += 1
            self._position = self._tags[step.loop_to] if isinstance(step.loop_to, str) else step.loop_to - 1
        else:
            self._loops_done[self._position] = 0
            self._position += 1

    @staticmethod
    def _step_duration(step: Step) -> float | None:
        if isinstance(step, VoltageScan):
            return abs(step.end_voltage_V - step.start_voltage_V) / (step.scan_rate_mV_per_s / 1000)
        return getattr(step, "until_time_s", None)

    def _time_to_step_limit(self, step: Step) -> float:
        duration = self._step_duration(step)
        return math.inf if duration is None else max(duration - self._step_time, 1e-9)

    def _step_ended(self, step: Step) -> bool:
        duration = self._step_duration(step)
        if duration is not None and self._step_time >= duration - 1e-9:
            return True
        if isinstance(step, ConstantCurrent) and step.until_voltage_V is not None:
            if self._current > 0 and self._voltage >= step.until_voltage_V:
                return True
            if self._current < 0 and self._voltage <= step.until_voltage_V:
                return True
        if isinstance(step, ConstantVoltage):
            thresholds = [
                (step.until_current_mA or 0.0) / 1000,
                (step.until_rate_C or 0.0) * self._cell.nominal_capacity,
            ]
            if any(threshold > 0 and abs(self._current) <= threshold for threshold in thresholds):
                return True
        return False

    # Electrochemistry.

    def _open_circuit_voltage(self) -> float:
        cell = self._cell
        return cell.empty_voltage + (cell.full_voltage - cell.empty_voltage) * self._state_of_charge

    def _integrate(self, step: Step, dt: float) -> None:
        open_circuit = self._open_circuit_voltage()
        resistance = self._cell.resistance

        if isinstance(step, OpenCircuitVoltage):
            current = 0.0
        elif isinstance(step, ConstantCurrent):
            current = step_current(step, self._cell.nominal_capacity)
        elif isinstance(step, ConstantVoltage):
            current = (step.voltage_V - open_circuit) / resistance
        elif isinstance(step, VoltageScan):
            direction = 1 if step.end_voltage_V > step.start_voltage_V else -1
            target = step.start_voltage_V + direction * step.scan_rate_mV_per_s / 1000 * (self._step_time + dt)
            current = (target - open_circuit) / resistance
        else:
            msg = f"The simulated cell cannot execute {type(step).__name__} steps."
            raise NotImplementedError(msg)

        current = max(-self._maximal_current, min(self._maximal_current, current))
        voltage = open_circuit + current * resistance
        charge = current * dt / 3600
        energy = voltage * charge

        self._state_of_charge = min(1.0, max(0.0, self._state_of_charge + charge / self._cell.nominal_capacity))
        if charge > 0:
            self._charging_capacity += charge
            self._charging_energy += energy
        else:
            self._discharging_capacity -= charge
            self._discharging_energy -= energy

        self._current = current
        self._voltage = voltage
        # The clock has nanosecond resolution, so summing time steps never drifts past a step or record boundary.
        self._step_time = round(self._step_time + dt, 9)
        self._test_time = round(self._test_time + dt, 9)

    def _check_safety(self) -> bool:
        safety = self._protocol.safety
        checks = [
            (safety.max_voltage_V is not None and self._voltage > safety.max_voltage_V, "maximal voltage"),
            (safety.min_voltage_V is not None and self._voltage < safety.min_voltage_V, "minimal voltage"),
            (safety.max_current_mA is not None and self._current * 1000 > safety.max_current_mA, "maximal current"),
            (safety.min_current_mA is not None and self._current * 1000 < safety.min_current_mA, "minimal current"),
            (
                safety.max_capacity_mAh is not None
                and max(self._charging_capacity, self._discharging_capacity) * 1000 > safety.max_capacity_mAh,
                "maximal capacity",
            ),
        ]
        for exceeded, limit in checks:
            if exceeded:
                self.faulted = True
                self.fault_reason = f"Safety limit '{limit}' exceeded at test time {self._test_time:g} s."
                return True
        return False

    def _record(self) -> DataPoint:
        c_rate = abs(self._current) / self._cell.nominal_capacity
        point = DataPoint(
            record_index=self._record_index,
            test_time=self._test_time,
            unix_time=self._start_unix_time + self._test_time,
            voltage=self._voltage,
            current=self._current,
            cycle_count=self._cycle_count,
            step_count=self._step_count,
            protocol_step=self._position + 1,
            charging_capacity=self._charging_capacity,
            discharging_capacity=self._discharging_capacity,
            charging_energy=self._charging_energy,
            discharging_energy=self._discharging_energy,
            ambient_temperature=self._cell.ambient_temperature,
            surface_temperature=self._cell.ambient_temperature + self._cell.heating * c_rate,
            temperature_t1=math.nan,
            temperature_t2=math.nan,
            temperature_t3=math.nan,
            temperature_t4=math.nan,
            temperature_t5=math.nan,
            ambient_pressure=math.nan,
            applied_pressure=math.nan,
            surface_pressure=math.nan,
            internal_resistance=math.nan,
            ac_internal_resistance=math.nan,
            dc_internal_resistance=math.nan,
            frequency=math.nan,
            real_impedance=math.nan,
            imaginary_impedance=math.nan,
        )
        self._record_index += 1
        self._next_record_time = self._test_time + self._record_interval()
        return point

    def _record_interval(self) -> float:
        step = self._steps[self._position] if self._position < len(self._steps) else None
        step_record = getattr(step, "record", None)
        interval = (step_record.time_s if step_record else None) or self._protocol.record.time_s
        return interval or DEFAULT_RECORD_INTERVAL
