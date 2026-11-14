"""Check authentic source parsing, direct CSV clock evidence and no-overwrite acquisition."""

import csv
from datetime import datetime, timezone

import pyarrow as arrow
import pyarrow.parquet as parquet
import pytest

from src.phase1_sources import (
    download_missing_files, inventory_sources, parse_weather_number, prepare_weather,
)


@pytest.mark.parametrize("text,value,quality", [
    ("", None, "missing"), ("T", None, "trace"), ("32s", None, "qualified_or_invalid"),
    ("NaN", None, "qualified_or_invalid"), ("0.00", 0.0, "numeric"),
])
def test_weather_missing_quality_and_real_zero_are_distinct(text, value, quality):
    assert parse_weather_number(text) == (value, quality)


def write_weather(path, date_text="2024-07-01T10:51:00", raw_report_column="RAW_REPORT"):
    """Write one invented LCDv1 CSV row whose raw METAR has an explicit UTC clock."""
    row = {"STATION": "72505394728", "DATE": date_text, "REPORT_TYPE": "FM-15",
           raw_report_column: "METAR KNYC 011551Z", "HourlyDryBulbTemperature": "68",
           "HourlyWindSpeed": "10", "HourlyVisibility": "10", "HourlyPrecipitation": "T",
           "HourlyPresentWeatherType": "||SN:02", "SOURCE": "7"}
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


@pytest.mark.parametrize("raw_report_column", ["RAW_REPORT", "REM"])
def test_csv_clock_is_verified_against_raw_report_not_civil_dst(tmp_path, raw_report_column):
    path = tmp_path / "weather.csv"
    write_weather(path, raw_report_column=raw_report_column)
    rows, proof = prepare_weather(path, "72505394728")
    assert proof["utc_clock_matches_by_month"] == {7: 1}
    assert rows[0]["observed_at_utc"] == datetime(2024, 7, 1, 15, 51, tzinfo=timezone.utc)
    assert rows[0]["temperature_c"] == pytest.approx(20)
    assert rows[0]["wind_metres_per_second"] == pytest.approx(4.4704)
    assert rows[0]["precipitation_previous_hour_mm"] is None
    assert rows[0]["precipitation_previous_hour_mm_quality"] == "trace"
    assert not rows[0]["has_verified_publication_time"]


def test_conflicting_csv_clock_fails_loudly(tmp_path):
    path = tmp_path / "weather.csv"
    write_weather(path, "2024-07-01T11:51:00")
    with pytest.raises(ValueError, match="clock conflicts"):
        prepare_weather(path, "72505394728")


def test_isolated_weather_clock_conflict_is_audited_without_guessing_a_repair(tmp_path):
    path = tmp_path / "weather.csv"
    write_weather(path)
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, good = reader.fieldnames, next(reader)
    bad = {**good, "DATE": "2024-07-01T10:59:00"}
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows([good, bad])
    original = path.read_bytes()
    rows, proof = prepare_weather(path, "72505394728")
    assert len(rows) == 1
    assert proof["counts"]["routine_clock_unresolved_rows_retained_in_raw_only"] == 1
    assert proof["clock_exceptions"][0]["issue"] == "csv_vs_raw_utc_clock_conflict"
    assert path.read_bytes() == original


def test_existing_valid_input_is_reused_without_network_or_changes(tmp_path, monkeypatch):
    path = tmp_path / "trips.parquet"
    parquet.write_table(arrow.table({"example": [1, 2, 3]}), path)
    before = path.read_bytes()
    def unexpected_download(*args, **kwargs):
        raise AssertionError("An existing valid copy must not be downloaded again")
    monkeypatch.setattr("urllib.request.urlopen", unexpected_download)
    source = {"source_id": "fixture", "kind": "trips", "path": str(path), "url": "https://example.invalid/trips.parquet"}
    assert download_missing_files([source])[0]["status"] == "checked"
    assert path.read_bytes() == before
    missing = {**source, "path": str(tmp_path / "missing.parquet")}
    assert inventory_sources([missing])[0]["status"] == "missing"
