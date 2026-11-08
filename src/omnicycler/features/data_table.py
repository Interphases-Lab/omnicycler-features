"""
Data records as an Arrow table: the binary bulk form of `MeasurementProvider.GetDataTable`.

Columns carry BDF preferred labels and only measured quantities; nothing is
derived or validated, unlike the BDF export.
"""

import dataclasses
import io
import math
import typing

import pyarrow as pa

from .types import DataPoint

COLUMNS: dict[str, str] = {
    "record_index": "Record Index / 1",
    "test_time": "Test Time / s",
    "unix_time": "Unix Time / s",
    "voltage": "Voltage / V",
    "current": "Current / A",
    "cycle_count": "Cycle Count / 1",
    "step_count": "Step Count / 1",
    "protocol_step": "Step ID",
    "charging_capacity": "Charging Capacity / Ah",
    "discharging_capacity": "Discharging Capacity / Ah",
    "charging_energy": "Charging Energy / Wh",
    "discharging_energy": "Discharging Energy / Wh",
    "ambient_temperature": "Ambient Temperature / degC",
    "surface_temperature": "Surface Temperature / degC",
    "temperature_t1": "Temperature T1 / degC",
    "temperature_t2": "Temperature T2 / degC",
    "temperature_t3": "Temperature T3 / degC",
    "temperature_t4": "Temperature T4 / degC",
    "temperature_t5": "Temperature T5 / degC",
    "ambient_pressure": "Ambient Pressure / Pa",
    "applied_pressure": "Applied Pressure / Pa",
    "surface_pressure": "Surface Pressure / Pa",
    "internal_resistance": "Internal Resistance / ohm",
    "ac_internal_resistance": "AC Internal Resistance / ohm",
    "dc_internal_resistance": "DC Internal Resistance / ohm",
    "frequency": "Frequency / Hz",
    "real_impedance": "Real Impedance / ohm",
    "imaginary_impedance": "Imaginary Impedance / ohm",
}
"""The BDF column of every `DataPoint` field."""

FIELDS_BY_LABEL = {label: field for field, label in COLUMNS.items()}
"""The `DataPoint` field of every BDF column."""

INTEGER_FIELDS = frozenset(
    field.name for field in dataclasses.fields(DataPoint) if typing.get_args(field.type)[:1] == (int,)
)
"""Fields that are whole numbers: indices and counters."""


def encode_table(points: list[DataPoint], labels: typing.Sequence[str] = ()) -> bytes:
    """
    Encode records as an Arrow IPC stream with one record batch.

    Args:
      points: Consecutive records, in record index order.
      labels: The BDF columns to include; empty for all of them.

    Returns:
      The stream. Integer fields are int64, the others float64. A value the
      channel did not measure (NaN) is null, and a column that is null in
      every row is left out.
    """
    fields = [FIELDS_BY_LABEL[label] for label in labels] if labels else list(COLUMNS)
    arrays: dict[str, pa.Array] = {}
    for field in fields:
        values = [getattr(point, field) for point in points]
        if field in INTEGER_FIELDS:
            arrays[COLUMNS[field]] = pa.array(values, type=pa.int64())
            continue
        array = pa.array(values, type=pa.float64(), from_pandas=True)  # NaN becomes null
        if len(array) and array.null_count == len(array):
            continue
        arrays[COLUMNS[field]] = array
    table = pa.table(arrays) if arrays else pa.table({})
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def decode_table(data: bytes) -> pa.Table:
    """Read an Arrow IPC stream from `GetDataTable` into a table."""
    with pa.ipc.open_stream(data) as reader:
        return reader.read_all()


def table_column(table: pa.Table, field: str) -> list[float]:
    """A column of a decoded table as floats, with NaN for missing values or a missing column."""
    label = COLUMNS[field]
    if label not in table.column_names:
        return [math.nan] * table.num_rows
    return [math.nan if value is None else float(value) for value in table.column(label).to_pylist()]


__all__ = ["COLUMNS", "FIELDS_BY_LABEL", "INTEGER_FIELDS", "decode_table", "encode_table", "table_column"]
