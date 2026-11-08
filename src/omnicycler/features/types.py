"""
Wire types shared by the standard features, the backends and the typed client.

Every structure and enum that crosses the SiLA wire is defined here, once.
This module imports only the standard library, `unitelabs.cdk.sila` and
`units`, so it stays a pure description of the wire.

Measured values a channel does not report are NaN, because SiLA has no
optional fields. `ChannelInformation.MeasuredQuantities` lists which of the
optional quantities a channel does report.
"""

import dataclasses
import enum
import math
import pathlib
import typing

from unitelabs.cdk import sila

from .units import AMPERE, AMPERE_HOUR, DEGREE_CELSIUS, HERTZ, OHM, PASCAL, SECOND, VOLT, WATT_HOUR

NonNegativeInteger = typing.Annotated[int, sila.constraints.MinimalInclusive(0)]

RunId = typing.Annotated[str, sila.constraints.MinimalLength(1), sila.constraints.MaximalLength(255)]
"""
The identifier of a run, chosen by the client that starts it.

Every command that acts on a run, and every read of its data, names the run
it expects, so a client never acts on or reads a run other than the one it
means. Use a fresh UUID unless you have your own unique identifier.
"""


class ChannelState(enum.Enum):
    """
    The state of a channel.

    Idle: No protocol run is held; the channel accepts `StartProtocol`.
    Running: A protocol is being executed.
    Paused: A protocol is paused and can be resumed.
    Finished: The protocol ended normally. Call `ClearChannel` before starting a new one.
    Stopped: The protocol was stopped by a client. Call `ClearChannel` before starting a new one.
    Faulted: The protocol ended because of a safety limit or a hardware fault. Call `ClearChannel` before starting a
      new one.
    Unavailable: The channel cannot be used, for example because it is offline or not installed.
    """

    IDLE = enum.auto()
    RUNNING = enum.auto()
    PAUSED = enum.auto()
    FINISHED = enum.auto()
    STOPPED = enum.auto()
    FAULTED = enum.auto()
    UNAVAILABLE = enum.auto()


class StepType(enum.Enum):
    """The technique steps of an aurora-unicycler protocol. Loop and tag steps are always supported."""

    OPEN_CIRCUIT_VOLTAGE = enum.auto()
    CONSTANT_CURRENT = enum.auto()
    CONSTANT_VOLTAGE = enum.auto()
    IMPEDANCE_SPECTROSCOPY = enum.auto()
    VOLTAGE_SCAN = enum.auto()


class ProtocolFormat(enum.Enum):
    """How the protocol of a run is encoded: as aurora-unicycler JSON, or in the cycler vendor's native format."""

    AURORA_UNICYCLER = enum.auto()
    NATIVE = enum.auto()


class BdfFormat(enum.Enum):
    """The file serialisation of a Battery Data Format export."""

    CSV = enum.auto()
    PARQUET = enum.auto()


class MeasuredQuantity(enum.Enum):
    """The optional measured quantities a channel may report in addition to the core quantities."""

    AMBIENT_TEMPERATURE = enum.auto()
    SURFACE_TEMPERATURE = enum.auto()
    TEMPERATURE_T1 = enum.auto()
    TEMPERATURE_T2 = enum.auto()
    TEMPERATURE_T3 = enum.auto()
    TEMPERATURE_T4 = enum.auto()
    TEMPERATURE_T5 = enum.auto()
    AMBIENT_PRESSURE = enum.auto()
    APPLIED_PRESSURE = enum.auto()
    SURFACE_PRESSURE = enum.auto()
    INTERNAL_RESISTANCE = enum.auto()
    AC_INTERNAL_RESISTANCE = enum.auto()
    DC_INTERNAL_RESISTANCE = enum.auto()
    FREQUENCY = enum.auto()
    REAL_IMPEDANCE = enum.auto()
    IMAGINARY_IMPEDANCE = enum.auto()


@dataclasses.dataclass
class Sample(sila.CustomDataType):
    """
    The cell under test.

    Attributes:
      Name: The name of the cell, used for the run and its data files.
      NominalCapacity: The nominal capacity of the cell, the reference for C-rates in the protocol.
      Identifier: An identifier of the cell outside OmniCycler, such as its serial number or a LIMS barcode, or
        empty if it has none. It is written to the export's metadata as the cell's serial number.
    """

    name: typing.Annotated[str, sila.constraints.MinimalLength(1), sila.constraints.MaximalLength(255)]
    nominal_capacity: typing.Annotated[float, AMPERE_HOUR, sila.constraints.MinimalExclusive(0.0)]
    identifier: typing.Annotated[str, sila.constraints.MaximalLength(255)] = ""


@dataclasses.dataclass
class ChannelInformation(sila.CustomDataType):
    """
    The static properties of one channel.

    Attributes:
      Channel: The channel number.
      MinimalVoltage: The lowest voltage the channel can apply or measure.
      MaximalVoltage: The highest voltage the channel can apply or measure.
      MaximalCurrent: The highest absolute current the channel can apply, in either direction.
      MeasuredQuantities: The optional quantities this channel reports. All other optional quantities are NaN.
    """

    channel: typing.Annotated[int, sila.constraints.MinimalInclusive(1)]
    minimal_voltage: typing.Annotated[float, VOLT]
    maximal_voltage: typing.Annotated[float, VOLT]
    maximal_current: typing.Annotated[float, AMPERE, sila.constraints.MinimalExclusive(0.0)]
    measured_quantities: list[MeasuredQuantity]


@dataclasses.dataclass(eq=False)
class ChannelStatus(sila.CustomDataType):
    """
    The current status of one channel.

    Attributes:
      Channel: The channel number.
      State: The state of the channel.
      RunId: The run the channel holds, or empty without a run. An idle channel never holds a run; a running,
        paused, finished, stopped or faulted channel always does. An unavailable channel may still hold one, whose
        data stays readable.
      Voltage: The latest measured voltage, or NaN if none is available.
      Current: The latest measured current, positive when charging, or NaN if none is available.
      TestTime: The time since the run started, or zero without a run.
      CycleCount: The cycle count of the run, or zero without a run.
      ProtocolStep: The position of the executing step in the protocol's method list, counting from one, or zero
        without a run.
      RecordCount: The number of data records of the run, or zero without a run.
      FaultReason: Why the protocol faulted, as the cycler reports it. Empty unless State is Faulted, and empty if
        the cycler gives no reason.
    """

    channel: typing.Annotated[int, sila.constraints.MinimalInclusive(1)]
    state: ChannelState
    run_id: str
    voltage: typing.Annotated[float, VOLT]
    current: typing.Annotated[float, AMPERE]
    test_time: typing.Annotated[float, SECOND]
    cycle_count: NonNegativeInteger
    protocol_step: NonNegativeInteger
    record_count: NonNegativeInteger
    fault_reason: str = ""

    @typing.override
    def __eq__(self, other: object) -> bool:
        """Compare field by field, treating NaN as equal to NaN so an unchanged status is not reported again."""
        if not isinstance(other, ChannelStatus):
            return NotImplemented
        return all(_same(getattr(self, field.name), getattr(other, field.name)) for field in dataclasses.fields(self))

    __hash__ = None  # a mutable dataclass with custom equality is unhashable


@dataclasses.dataclass
class RunInformation(sila.CustomDataType):
    """
    The run a channel currently holds.

    Attributes:
      RunId: The identifier the client gave the run when it started it.
      Sample: The cell under test.
      ProtocolFormat: How Protocol is encoded.
      Protocol: The protocol as executed, with the sample merged in for aurora-unicycler protocols.
      StartTime: The Unix time at which the run started.
    """

    run_id: typing.Annotated[str, sila.constraints.MinimalLength(1)]
    sample: Sample
    protocol_format: ProtocolFormat
    protocol: bytes
    start_time: typing.Annotated[float, SECOND]


@dataclasses.dataclass
class DataPoint(sila.CustomDataType):
    """
    One data record of a run, in Battery Data Format quantities.

    Attributes:
      RecordIndex: The position of this record in the run, counting from zero without gaps. A record keeps its
        index for as long as the run exists.
      TestTime: The time since the run started.
      UnixTime: The Unix time of the measurement.
      Voltage: The measured voltage.
      Current: The measured current, positive when charging the cell.
      CycleCount: The cycle count.
      StepCount: The number of steps started since the run started.
      ProtocolStep: The position of the executing step in the protocol's method list, counting from one.
      ChargingCapacity: The charge put into the cell since the run started.
      DischargingCapacity: The charge taken out of the cell since the run started.
      ChargingEnergy: The energy put into the cell since the run started.
      DischargingEnergy: The energy taken out of the cell since the run started.
      AmbientTemperature: The ambient temperature, or NaN if not measured.
      SurfaceTemperature: The temperature at the cell surface, or NaN if not measured.
      TemperatureT1: The temperature of auxiliary sensor T1, or NaN if not measured.
      TemperatureT2: The temperature of auxiliary sensor T2, or NaN if not measured.
      TemperatureT3: The temperature of auxiliary sensor T3, or NaN if not measured.
      TemperatureT4: The temperature of auxiliary sensor T4, or NaN if not measured.
      TemperatureT5: The temperature of auxiliary sensor T5, or NaN if not measured.
      AmbientPressure: The ambient pressure, or NaN if not measured.
      AppliedPressure: The pressure an external fixture applies to the cell, or NaN if not measured.
      SurfacePressure: The pressure measured at the cell surface, or NaN if not measured.
      InternalResistance: The internal resistance as the cycler reports it, or NaN if not measured.
      AcInternalResistance: The AC internal resistance, or NaN if not measured.
      DcInternalResistance: The DC internal resistance, or NaN if not measured.
      Frequency: The excitation frequency of an impedance measurement, or NaN outside one.
      RealImpedance: The real part of the measured impedance, or NaN outside an impedance measurement.
      ImaginaryImpedance: The imaginary part of the measured impedance, negative when capacitive, or NaN outside
        an impedance measurement.
    """

    record_index: NonNegativeInteger
    test_time: typing.Annotated[float, SECOND]
    unix_time: typing.Annotated[float, SECOND]
    voltage: typing.Annotated[float, VOLT]
    current: typing.Annotated[float, AMPERE]
    cycle_count: NonNegativeInteger
    step_count: NonNegativeInteger
    protocol_step: NonNegativeInteger
    charging_capacity: typing.Annotated[float, AMPERE_HOUR]
    discharging_capacity: typing.Annotated[float, AMPERE_HOUR]
    charging_energy: typing.Annotated[float, WATT_HOUR]
    discharging_energy: typing.Annotated[float, WATT_HOUR]
    ambient_temperature: typing.Annotated[float, DEGREE_CELSIUS]
    surface_temperature: typing.Annotated[float, DEGREE_CELSIUS]
    temperature_t1: typing.Annotated[float, DEGREE_CELSIUS]
    temperature_t2: typing.Annotated[float, DEGREE_CELSIUS]
    temperature_t3: typing.Annotated[float, DEGREE_CELSIUS]
    temperature_t4: typing.Annotated[float, DEGREE_CELSIUS]
    temperature_t5: typing.Annotated[float, DEGREE_CELSIUS]
    ambient_pressure: typing.Annotated[float, PASCAL]
    applied_pressure: typing.Annotated[float, PASCAL]
    surface_pressure: typing.Annotated[float, PASCAL]
    internal_resistance: typing.Annotated[float, OHM]
    ac_internal_resistance: typing.Annotated[float, OHM]
    dc_internal_resistance: typing.Annotated[float, OHM]
    frequency: typing.Annotated[float, HERTZ]
    real_impedance: typing.Annotated[float, OHM]
    imaginary_impedance: typing.Annotated[float, OHM]


@dataclasses.dataclass(frozen=True)
class BdfExport:
    """
    A Battery Data Format export of one run: the two responses of `ExportData`.

    Attributes:
      data: The BDF table, as CSV or Parquet.
      metadata: The BattINFO metadata sidecar as UTF-8 JSON, in the form batterydf writes and reads it.
    """

    data: bytes
    metadata: bytes

    def save(self, path: str | pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        """
        Write the table and its sidecar where batterydf looks for them.

        Args:
          path: The table file, such as `cell-1.bdf.csv`. The sidecar is written beside it with the last suffix
            replaced, as `cell-1.bdf.metadata.json`, so `bdf.read` finds it.

        Returns:
          The paths of the table and the sidecar.
        """
        table = pathlib.Path(path)
        sidecar = table.with_suffix(".metadata.json")
        table.write_bytes(self.data)
        sidecar.write_bytes(self.metadata)
        return table, sidecar


def _same(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left) and math.isnan(right):
        return True
    return left == right


__all__ = [
    "BdfExport",
    "BdfFormat",
    "ChannelInformation",
    "ChannelState",
    "ChannelStatus",
    "DataPoint",
    "MeasuredQuantity",
    "ProtocolFormat",
    "RunId",
    "RunInformation",
    "Sample",
    "StepType",
]
