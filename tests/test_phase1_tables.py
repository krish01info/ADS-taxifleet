"""Verify totals, money denominators, coverage zeros, joins and training-only OD statistics."""

from datetime import datetime, timedelta

import pytest
from pyspark.sql import functions as F

from src.phase1_tables import (
    add_trip_time_fields, aggregate_zone_activity, build_training_pair_reference,
    build_zone_hour_table, check_unique_keys, join_reference_tables,
    reconcile_activity, unresolved_event_counts,
)


def flagged_trips(spark, changes):
    """Build tiny invented already-cleaned rows, independent of downloaded trip data."""
    rows = []
    for patch in changes:
        request = patch.get("request_datetime", datetime(2024, 1, 1, 12))
        row = {"service_license": "HV0003", "request_datetime": request,
               "pickup_datetime": request + timedelta(minutes=1),
               "dropoff_datetime": request + timedelta(minutes=11),
               "pickup_zone_id": 1, "dropoff_zone_id": 2, "trip_miles": 2.0,
               "trip_seconds": 600, "base_fare_usd": 10.0, "tolls_usd": 0.0,
               "is_accepted": True, "is_demand_eligible": True, "is_simulation_eligible": True,
               "is_fare_analysis_eligible": True, "is_monetary_eligible": True,
               "is_valid_tolls_usd": True, "pickup_zone_status": "mapped", "dropoff_zone_status": "mapped",
               "has_missing_dropoff_datetime": False, "has_invalid_dropoff_order": False}
        row.update(patch)
        rows.append(row)
    schema = ("service_license string, request_datetime timestamp_ntz, pickup_datetime timestamp_ntz, "
              "dropoff_datetime timestamp_ntz, pickup_zone_id long, dropoff_zone_id long, trip_miles double, "
              "trip_seconds long, base_fare_usd double, tolls_usd double, is_accepted boolean, "
              "is_demand_eligible boolean, is_simulation_eligible boolean, is_fare_analysis_eligible boolean, "
              "is_monetary_eligible boolean, is_valid_tolls_usd boolean, pickup_zone_status string, "
              "dropoff_zone_status string, has_missing_dropoff_datetime boolean, has_invalid_dropoff_order boolean")
    return add_trip_time_fields(spark.createDataFrame(rows, schema=schema))


def tiny_zones(spark):
    return spark.createDataFrame([(1, 0.0, 0.0), (2, 1609.344, 0.0)],
                                 "zone_id long, centroid_x_metres double, centroid_y_metres double")


def test_activity_totals_and_valid_money_denominators(phase1_spark):
    trips = flagged_trips(phase1_spark, [{},
        {"base_fare_usd": -5.0, "is_fare_analysis_eligible": False, "is_monetary_eligible": False,
         "dropoff_zone_id": 265, "dropoff_zone_status": "recognised_unmapped", "is_simulation_eligible": False},
        {"base_fare_usd": 30.0, "trip_seconds": 300}])
    partials = aggregate_zone_activity(trips)
    row = partials.filter(F.col("zone_id") == 1).first()
    assert row.request_count_observed == row.pickup_count_observed == 3
    assert row.valid_fare_count == 2 and row.valid_base_fare_sum_usd == 40
    assert row.fare_per_minute_sum == 7  # 10/10 + 30/5, not pooled fare/pooled minutes.
    assert partials.agg(F.sum("dropoff_count_observed")).first()[0] == 2
    audit = reconcile_activity(trips, partials, unresolved_event_counts(trips))
    assert audit["request"]["resolved_in_scope"] == 3


def test_empty_coverage_and_missing_coverage_are_not_interchanged(phase1_spark):
    trips = flagged_trips(phase1_spark, [{"base_fare_usd": None, "is_fare_analysis_eligible": False,
                                        "is_monetary_eligible": False}])
    partials, unresolved = aggregate_zone_activity(trips), unresolved_event_counts(trips)
    complete = build_zone_hour_table(phase1_spark, tiny_zones(phase1_spark), partials, unresolved,
        ["2023-12", "2024-01", "2024-02"], end_civil="2024-01-02")
    actual = complete.filter((F.col("zone_id") == 1) & (F.col("hour_nyc") == 12)).first()
    assert actual.request_count == 1
    assert actual.valid_fare_count == 0 and actual.mean_base_fare_usd is None
    assert actual.valid_base_fare_sum_usd is None
    empty = complete.filter((F.col("zone_id") == 2) & (F.col("hour_nyc") == 12)).first()
    assert empty.request_count == 0 and empty.mean_base_fare_usd is None
    missing = build_zone_hour_table(phase1_spark, tiny_zones(phase1_spark), partials, unresolved,
        ["2024-01"], end_civil="2024-01-02")
    assert missing.filter(F.col("request_count").isNotNull()).count() == 0
    assert missing.agg(F.sum("request_count_observed")).first()[0] == 1


def test_non_unique_reference_is_rejected_before_multiplication(phase1_spark):
    facts = phase1_spark.createDataFrame([(1,)], "zone_id int")
    reference = phase1_spark.createDataFrame([(1, "a"), (1, "b")], "zone_id int, label string")
    with pytest.raises(ValueError, match="Repeated key"):
        join_reference_tables(facts, reference, ["zone_id"])
    nullable = phase1_spark.createDataFrame([(None,)], "zone_id int")
    with pytest.raises(ValueError, match="Null key"):
        check_unique_keys(nullable, ["zone_id"])


def test_training_reference_uses_pooled_trips_not_monthly_medians_or_future_outcomes(phase1_spark):
    changes = [{"trip_miles": 1.0}]
    for _ in range(3):
        changes.append({"request_datetime": datetime(2024, 2, 1, 12), "trip_miles": 9.0})
    changes += [
        {"request_datetime": datetime(2024, 9, 1), "trip_miles": 1000.0},
        {"request_datetime": datetime(2024, 11, 1), "trip_miles": 2000.0},
        {"request_datetime": datetime(2025, 1, 1), "trip_miles": 3000.0},
        {"request_datetime": datetime(2024, 8, 31, 23, 59), "dropoff_datetime": datetime(2024, 9, 1, 0, 10), "trip_miles": 4000.0},
    ]
    reference = build_training_pair_reference(flagged_trips(phase1_spark, changes), tiny_zones(phase1_spark))
    row = reference.filter((F.col("pickup_zone_id") == 1) & (F.col("dropoff_zone_id") == 2)).first()
    assert row.trip_count == 4 and row.median_passenger_miles == 9
    assert row.support_status == "sparse" and row.support_threshold == 30
    unsupported = reference.filter((F.col("pickup_zone_id") == 2) & (F.col("dropoff_zone_id") == 1)).first()
    assert unsupported.trip_count == 0 and unsupported.median_passenger_miles is None
    assert unsupported.straight_line_centroid_miles == pytest.approx(1)


def test_dst_events_reconcile_without_changing_demand_flag(phase1_spark):
    trips = flagged_trips(phase1_spark, [{"request_datetime": datetime(2024, 11, 3, 1, 30)},
                                       {"request_datetime": datetime(2024, 3, 10, 2, 30)}])
    partials, unresolved = aggregate_zone_activity(trips), unresolved_event_counts(trips)
    audit = reconcile_activity(trips, partials, unresolved)
    assert audit["request"]["eligible"] == audit["request"]["unresolved_in_scope"] == 2
    assert trips.filter(~F.col("is_demand_eligible")).count() == 0


def test_saved_utc_contract_and_existing_output_preservation(phase1_spark, tmp_path):
    import pyarrow.parquet as parquet
    from notebooks.run_phase1 import save_table
    table = phase1_spark.range(1).select(F.lit("2024-07-01T16:00:00Z").cast("timestamp").alias("hour_start_utc"))
    destination = tmp_path / "saved"
    result = save_table(table, destination)
    assert result["rows"] == 1
    file = next(destination.glob("*.parquet"))
    assert parquet.ParquetFile(file).schema_arrow.field("hour_start_utc").type.tz == "UTC"
    before = file.read_bytes()
    with pytest.raises(Exception, match="(?i)already exists"):
        save_table(table, destination)
    assert file.read_bytes() == before
