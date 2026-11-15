"""Test civil/UTC/EST alignment, DST ambiguity, leap day and split boundaries."""

from datetime import datetime, timezone

import pytest

from src.phase1_time import add_event_time_keys, build_verified_hour_grid, classify_local_time


@pytest.mark.parametrize("value,status,utc_hour", [
    (datetime(2024, 1, 1, 12), "resolved", 17),
    (datetime(2024, 7, 1, 12), "resolved", 16),
    (datetime(2024, 2, 29, 12), "resolved", 17),
    (datetime(2024, 3, 10, 2, 30), "nonexistent", None),
    (datetime(2024, 11, 3, 1, 30), "ambiguous", None),
    (None, "missing", None),
])
def test_local_clock_classification(value, status, utc_hour):
    actual, instant = classify_local_time(value)
    assert actual == status
    assert (instant.hour if instant else None) == utc_hour


def test_offset_aware_input_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="offset-free"):
        classify_local_time(datetime(2024, 1, 1, tzinfo=timezone.utc))


def test_spark_keys_preserve_civil_split_and_do_not_guess_fold(phase1_spark):
    source = phase1_spark.createDataFrame([
        (datetime(2024, 9, 1, 0, 30), True),
        (datetime(2024, 11, 3, 1, 30), True),
        (datetime(2024, 3, 10, 2, 30), True),
        (datetime(2025, 1, 1, 0), True),
    ], "request_datetime timestamp_ntz, is_demand_eligible boolean")
    rows = add_event_time_keys(source, "request_datetime", "request").collect()
    assert rows[0].request_period == "validation"
    assert rows[0].request_hour_est == datetime(2024, 8, 31, 23)
    assert rows[0].request_hour_utc == datetime(2024, 9, 1, 4)
    assert rows[1].request_time_status == "ambiguous" and rows[1].request_utc is None
    assert rows[2].request_time_status == "nonexistent" and rows[2].request_utc is None
    assert rows[3].request_period == "drift"
    assert all(row.is_demand_eligible for row in rows)


def test_grid_counts_real_hours_and_keeps_utc_key_unique(phase1_spark):
    zones = phase1_spark.createDataFrame([(1,)], "zone_id int")
    assert build_verified_hour_grid(phase1_spark, zones, "2024-03-10", "2024-03-11").count() == 23
    autumn = build_verified_hour_grid(phase1_spark, zones, "2024-11-03", "2024-11-04")
    assert autumn.count() == autumn.select("hour_start_utc").distinct().count() == 25
    assert build_verified_hour_grid(phase1_spark, zones, "2024-01-01", "2025-01-01").count() == 8784
