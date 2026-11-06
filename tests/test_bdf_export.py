import dataclasses
import io
import math

import polars as pl
import pytest
from bdf import Metadata, validate_df
from bdf.spec import COLUMN_ONTOLOGY

from omnicycler.features.bdf_export import COLUMNS, DERIVED_COLUMNS, encode_bdf, to_frame, validate_frame
from omnicycler.features.simulation import ProtocolSimulation
from omnicycler.features.types import BdfFormat, DataPoint
from tests.helpers import make_protocol

KNOWN_LABELS = set(COLUMN_ONTOLOGY.required_labels()) | set(COLUMN_ONTOLOGY.optional_labels())


@pytest.fixture
def points() -> list[DataPoint]:
    protocol = make_protocol(
        [
            {"step": "tag", "tag": "cycle"},
            {"step": "constant_current", "rate_C": 1, "until_time_s": 120},
            {"step": "constant_current", "rate_C": -1, "until_time_s": 60},
            {"step": "loop", "loop_to": "cycle", "cycle_count": 2},
        ]
    )
    return ProtocolSimulation(protocol, start_unix_time=1_700_000_000.0).advance(1e6)


def test_every_column_is_a_bdf_column():
    assert set(COLUMNS.values()) <= KNOWN_LABELS
    assert set(DERIVED_COLUMNS) <= KNOWN_LABELS
    assert set(COLUMN_ONTOLOGY.required_labels()) <= set(COLUMNS.values())
    assert not set(COLUMNS.values()) & set(DERIVED_COLUMNS)


def test_every_data_point_field_is_exported():
    assert set(COLUMNS) == {field.name for field in dataclasses.fields(DataPoint)}


def test_all_nan_columns_are_dropped(points):
    frame = to_frame(points)

    assert "Temperature T2 / degC" not in frame.columns
    assert "Real Impedance / ohm" not in frame.columns
    assert "Phase / deg" not in frame.columns
    assert "Surface Temperature / degC" in frame.columns
    assert not any(frame[name].is_nan().all() for name, dtype in frame.schema.items() if dtype.is_float())


def test_totals_follow_bdf(points):
    last = to_frame(points).row(-1, named=True)

    charged, discharged = last["Charging Capacity / Ah"], last["Discharging Capacity / Ah"]
    assert last["Net Capacity / Ah"] == pytest.approx(charged - discharged)
    assert last["Cumulative Capacity / Ah"] == pytest.approx(charged + discharged)
    assert last["Power / W"] == pytest.approx(last["Voltage / V"] * last["Current / A"])


def test_step_and_cycle_values_count_from_their_start(points):
    frame = to_frame(points)

    # 1C on a 1 Ah cell: 120 s charge (1/30 Ah), then 60 s discharge (1/60 Ah), twice.
    ends = frame.group_by("Step Count / 1", maintain_order=True).last()
    assert ends["Step Time / s"].to_list() == pytest.approx([120, 60, 120, 60])
    assert ends["Step Charging Capacity / Ah"].to_list() == pytest.approx([1 / 30, 0, 1 / 30, 0])
    assert ends["Step Discharging Capacity / Ah"].to_list() == pytest.approx([0, 1 / 60, 0, 1 / 60])

    cycles = frame.group_by("Cycle Count / 1", maintain_order=True).last()
    assert cycles["Cycle Net Capacity / Ah"].to_list() == pytest.approx([1 / 60, 1 / 60])
    assert cycles["Cycle Cumulative Capacity / Ah"].to_list() == pytest.approx([1 / 20, 1 / 20])

    first_of_step = frame.filter(pl.col("Step Record Index / 1") == 1)
    assert first_of_step["Step Count / 1"].to_list() == [1, 2, 3, 4]


def test_impedance_magnitude_and_phase(points):
    points[3] = dataclasses.replace(points[3], frequency=1000.0, real_impedance=0.03, imaginary_impedance=-0.04)
    frame = to_frame(points)

    assert frame["Absolute Impedance / ohm"][3] == pytest.approx(0.05)
    assert frame["Phase / deg"][3] == pytest.approx(math.degrees(math.atan2(-0.04, 0.03)))
    assert math.isnan(frame["Absolute Impedance / ohm"][4])


def test_exported_table_passes_batterydf_including_derived_checks(points):
    validate_frame(to_frame(points))


def test_inconsistent_derived_column_is_caught(points):
    broken = to_frame(points).with_columns(pl.col("Net Capacity / Ah") + 1)

    with pytest.raises(ValueError, match="inconsistent derived"):
        validate_frame(broken)


@pytest.mark.parametrize("data_format", list(BdfFormat))
def test_files_are_valid_bdf(points, data_format):
    data = encode_bdf(points, data_format, Metadata()).data

    reader = pl.read_csv if data_format is BdfFormat.CSV else pl.read_parquet
    frame = reader(io.BytesIO(data))
    report = validate_df(frame, raise_on_error=True)
    assert report["extras"] == []
    assert report["derived"]["issues"] == []
    assert len(frame) == len(points)
    assert frame["Voltage / V"].to_list() == pytest.approx([point.voltage for point in points])


def test_csv_header_uses_preferred_labels(points):
    header = encode_bdf(points, BdfFormat.CSV, Metadata()).data.decode().splitlines()[0]

    assert header.startswith("Record Index / 1,Test Time / s,Unix Time / s,Voltage / V,Current / A")


def test_empty_metadata_gives_an_empty_sidecar(points):
    assert encode_bdf(points, BdfFormat.CSV, Metadata()).metadata == b"{}"
