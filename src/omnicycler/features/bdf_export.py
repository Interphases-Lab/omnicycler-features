"""
Battery Data Format (BDF) export, shared by every OmniCycler server.

Built on batterydf 0.2 (BDF ontology 1.3) and polars. Brand backends report measured
quantities only; every BDF column that follows from them is calculated here,
once for every brand, by the ontology's own definitions, so the same run
gives the same file whichever cycler produced it. Columns that are NaN
throughout are left out of the file.
"""

import dataclasses
import pathlib
import tempfile
import warnings

import bdf
import polars as pl
from bdf import BDFValidationError, validate_df

from .data_table import COLUMNS
from .types import BdfExport, BdfFormat, DataPoint

_PER_SEGMENT = {"Step": "Step Count / 1", "Cycle": "Cycle Count / 1"}
_COUNTERS = {
    "Charging Capacity": "Charging Capacity / Ah",
    "Discharging Capacity": "Discharging Capacity / Ah",
    "Charging Energy": "Charging Energy / Wh",
    "Discharging Energy": "Discharging Energy / Wh",
}

DERIVED_COLUMNS = (
    "Net Capacity / Ah",
    "Cumulative Capacity / Ah",
    "Net Energy / Wh",
    "Cumulative Energy / Wh",
    "Power / W",
    "Step Time / s",
    "Step Record Index / 1",
    "Step Charging Capacity / Ah",
    "Step Discharging Capacity / Ah",
    "Step Net Capacity / Ah",
    "Step Cumulative Capacity / Ah",
    "Step Charging Energy / Wh",
    "Step Discharging Energy / Wh",
    "Step Net Energy / Wh",
    "Step Cumulative Energy / Wh",
    "Cycle Charging Capacity / Ah",
    "Cycle Discharging Capacity / Ah",
    "Cycle Net Capacity / Ah",
    "Cycle Cumulative Capacity / Ah",
    "Cycle Charging Energy / Wh",
    "Cycle Discharging Energy / Wh",
    "Cycle Net Energy / Wh",
    "Cycle Cumulative Energy / Wh",
    "Absolute Impedance / ohm",
    "Phase / deg",
)
"""BDF columns calculated from the measured ones."""

_FILE_NAMES = {BdfFormat.CSV: "data.bdf.csv", BdfFormat.PARQUET: "data.bdf.parquet"}


def to_frame(points: list[DataPoint]) -> pl.DataFrame:
    """
    Build a BDF table from the records of one run.

    Adds the derived columns by their BDF ontology definitions: net is
    charging minus discharging, cumulative is charging plus discharging,
    power is voltage times current, step and cycle values count from the
    start of the step or cycle, and impedance magnitude and phase follow
    from its real and imaginary parts. Columns that are NaN in every row are
    dropped.

    Args:
      points: The records of one run, in record index order.

    Returns:
      A table with BDF preferred labels as column names.
    """
    frame = pl.DataFrame(
        [dataclasses.astuple(point) for point in points],
        schema=[field.name for field in dataclasses.fields(DataPoint)],
        orient="row",
    ).rename(COLUMNS)
    frame = frame.with_columns(
        (pl.col("Charging Capacity / Ah") - pl.col("Discharging Capacity / Ah")).alias("Net Capacity / Ah"),
        (pl.col("Charging Capacity / Ah") + pl.col("Discharging Capacity / Ah")).alias("Cumulative Capacity / Ah"),
        (pl.col("Charging Energy / Wh") - pl.col("Discharging Energy / Wh")).alias("Net Energy / Wh"),
        (pl.col("Charging Energy / Wh") + pl.col("Discharging Energy / Wh")).alias("Cumulative Energy / Wh"),
        (pl.col("Voltage / V") * pl.col("Current / A")).alias("Power / W"),
        (pl.col("Real Impedance / ohm").pow(2) + pl.col("Imaginary Impedance / ohm").pow(2))
        .sqrt()
        .alias("Absolute Impedance / ohm"),
        pl.arctan2(pl.col("Imaginary Impedance / ohm"), pl.col("Real Impedance / ohm")).degrees().alias("Phase / deg"),
    )
    frame = frame.with_columns(
        (pl.col("Test Time / s") - _at_segment_start("Test Time / s", "Step Count / 1")).alias("Step Time / s"),
        pl.int_range(1, pl.len() + 1).over(_segment("Step Count / 1")).alias("Step Record Index / 1"),
        *_segment_counters(),
    )
    frame = frame.with_columns(*_segment_totals())
    nan_only = [
        name for name, dtype in frame.schema.items() if dtype.is_float() and len(frame) and frame[name].is_nan().all()
    ]
    return frame.drop(nan_only)


def validate_frame(frame: pl.DataFrame) -> None:
    """
    Validate a BDF table with batterydf, including the consistency of its derived columns.

    Raises:
      ValueError: If batterydf rejects the table, reports a column it does not
        know, or finds a derived column inconsistent with its definition.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            report = validate_df(frame, raise_on_error=True)
    except BDFValidationError as error:
        msg = f"The export is not a valid BDF table: {error}"
        raise ValueError(msg) from error

    if report["extras"]:
        msg = f"The export contains columns batterydf does not know: {report['extras']}"
        raise ValueError(msg)
    if issues := report["derived"]["issues"]:
        msg = f"The export has inconsistent derived columns: {issues}"
        raise ValueError(msg)


def encode_bdf(points: list[DataPoint], data_format: BdfFormat, metadata: bdf.Metadata) -> BdfExport:
    """
    Encode the records of one run as a BDF file with its metadata sidecar.

    batterydf writes both files, so the sidecar carries its version stamps
    and reads back with `bdf.read`.

    Args:
      points: The records of one run, in record index order.
      data_format: CSV or Parquet.
      metadata: The run's metadata, from `bdf_metadata.build_metadata`.

    Returns:
      The table, with BDF preferred labels as column names, and the sidecar.

    Raises:
      ValueError: If the resulting table is not valid BDF.
    """
    frame = to_frame(points)
    validate_frame(frame)
    with tempfile.TemporaryDirectory(prefix="omnicycler-bdf-") as directory:
        path = pathlib.Path(directory) / _FILE_NAMES[data_format]
        bdf.save(frame, path, metadata=metadata, labels="preferred", validate=False)
        sidecar = path.with_suffix(".metadata.json")  # batterydf writes none for empty metadata
        return BdfExport(data=path.read_bytes(), metadata=sidecar.read_bytes() if sidecar.exists() else b"{}")


def _segment_begins(counter: str) -> pl.Expr:
    """Whether a row is the first of a step or cycle, i.e. its counter differs from the previous row's."""
    return (pl.col(counter) != pl.col(counter).shift(1)).fill_null(value=True)


def _segment(counter: str) -> pl.Expr:
    """Number the consecutive runs of equal counter values, so a step or cycle is one segment."""
    return _segment_begins(counter).cum_sum()


def _at_segment_start(column: str, counter: str) -> pl.Expr:
    """The value a column had when the current segment began: its last value in the previous segment, or zero."""
    previous = pl.col(column).shift(1).fill_null(0.0)
    return pl.when(_segment_begins(counter)).then(previous).otherwise(None).forward_fill()


def _segment_counters() -> list[pl.Expr]:
    return [
        (pl.col(column) - _at_segment_start(column, counter)).alias(f"{segment} {name} / {column.rsplit(' / ', 1)[1]}")
        for segment, counter in _PER_SEGMENT.items()
        for name, column in _COUNTERS.items()
    ]


def _segment_totals() -> list[pl.Expr]:
    expressions = []
    for segment in _PER_SEGMENT:
        for quantity, unit in (("Capacity", "Ah"), ("Energy", "Wh")):
            charging = pl.col(f"{segment} Charging {quantity} / {unit}")
            discharging = pl.col(f"{segment} Discharging {quantity} / {unit}")
            expressions.append((charging - discharging).alias(f"{segment} Net {quantity} / {unit}"))
            expressions.append((charging + discharging).alias(f"{segment} Cumulative {quantity} / {unit}"))
    return expressions


__all__ = ["COLUMNS", "DERIVED_COLUMNS", "encode_bdf", "to_frame", "validate_frame"]
