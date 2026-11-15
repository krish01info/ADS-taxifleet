"""Check the first cleaning step on invented trips, without the downloaded data."""

import os
import sys
from datetime import datetime, timedelta

import pytest
from pyspark.sql import SparkSession

from src.trip_cleaning import COLLISION_KEY, MONEY_COLUMNS, clean_trips

RAW_SCHEMA = """
    hvfhs_license_num string, dispatching_base_num string, originating_base_num string,
    request_datetime timestamp, pickup_datetime timestamp, dropoff_datetime timestamp,
    PULocationID int, DOLocationID int, trip_miles double, trip_time long,
    base_passenger_fare double, tolls double, bcf double, sales_tax double,
    congestion_surcharge double, airport_fee double, tips double, driver_pay double
"""
KNOWN_ZONE_IDS = [1, 79, 137, 158, 161, 186, 264, 265]


@pytest.fixture(scope="module")
def spark():
    """Start a small local Spark session for synthetic cleaning checks."""
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    session = (
        SparkSession.builder.master("local[2]").appName("trip-cleaning-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "2")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def make_trip(**changes):
    """Return an invented valid raw TLC trip with optional field changes."""
    pickup = datetime(2024, 1, 1, 12)
    trip = {
        "hvfhs_license_num": "HV0003", "dispatching_base_num": "B00001",
        "originating_base_num": None, "request_datetime": pickup - timedelta(minutes=2),
        "pickup_datetime": pickup, "dropoff_datetime": pickup + timedelta(minutes=10),
        "PULocationID": 161, "DOLocationID": 158, "trip_miles": 2.0, "trip_time": 600,
        "base_passenger_fare": 20.0, "tolls": 0.0, "bcf": 0.5, "sales_tax": 1.0,
        "congestion_surcharge": 2.75, "airport_fee": 0.0, "tips": 3.0, "driver_pay": 12.0,
    }
    trip.update(changes)
    return trip


def inspect_trips(spark, raw_rows):
    """Clean a tiny synthetic table and collect its results for assertions."""
    raw = spark.createDataFrame(raw_rows, schema=RAW_SCHEMA)
    return clean_trips(raw, KNOWN_ZONE_IDS).collect()


def test_inclusive_duration_and_speed_boundaries(spark):
    cases = [
        (60, 1.0 / 60, True), (10800, 240.0, True),
        (59, 1.0, False), (10801, 10.0, False),
        (3600, 0.999, False), (3600, 80.001, False),
        (3600, 1.0, True), (3600, 80.0, True),
    ]
    for seconds, miles, expected in cases:
        row = inspect_trips(spark, [make_trip(trip_time=seconds, trip_miles=miles)])[0]
        assert row.is_accepted is expected
        assert row.cleaning_status == ("accepted" if expected else "quarantined")


def test_nonpositive_and_missing_duration_do_not_divide_by_zero(spark):
    for seconds in [0, -1, None]:
        row = inspect_trips(spark, [make_trip(trip_time=seconds)])[0]
        assert row.speed_mph is None
        assert row.is_accepted is False


def test_request_must_be_strictly_before_pickup(spark):
    pickup = make_trip()["pickup_datetime"]
    for request in [None, pickup, pickup + timedelta(seconds=1)]:
        row = inspect_trips(spark, [make_trip(request_datetime=request)])[0]
        assert not row.is_accepted


def test_missing_and_nonfinite_miles_are_quarantined(spark):
    for miles in [None, float("nan"), float("inf"), -1.0]:
        row = inspect_trips(spark, [make_trip(trip_miles=miles)])[0]
        assert not row.is_accepted


def test_timestamp_disagreement_is_flagged_without_changing_duration_filter(spark):
    pickup = make_trip()["pickup_datetime"]
    row = inspect_trips(spark, [make_trip(dropoff_datetime=pickup + timedelta(seconds=601))])[0]
    assert row.duration_difference_seconds == 1
    assert row.has_timestamp_duration_disagreement
    assert row.speed_mph == 12
    assert row.is_accepted and row.is_simulation_eligible


@pytest.mark.parametrize("destination,status", [
    (264, "recognised_unmapped"), (265, "recognised_unmapped"),
    (999, "unrecognised"), (None, "missing"),
])
def test_unmapped_destination_preserves_pickup_demand(spark, destination, status):
    row = inspect_trips(spark, [make_trip(DOLocationID=destination)])[0]
    assert row.dropoff_zone_status == status
    assert row.is_accepted and row.is_demand_eligible
    assert not row.is_simulation_eligible


@pytest.mark.parametrize("pickup,status", [
    (264, "recognised_unmapped"), (265, "recognised_unmapped"),
    (999, "unrecognised"), (None, "missing"),
])
def test_unmapped_pickup_is_not_mapped_demand(spark, pickup, status):
    row = inspect_trips(spark, [make_trip(PULocationID=pickup)])[0]
    assert row.pickup_zone_status == status
    assert not row.is_demand_eligible
    assert not row.is_simulation_eligible
    assert row.is_accepted is (pickup is not None)


def test_missing_dropoff_and_service_license_keep_usable_demand(spark):
    row = inspect_trips(spark, [make_trip(dropoff_datetime=None, hvfhs_license_num=None)])[0]
    assert row.is_demand_eligible
    assert not row.is_simulation_eligible
    assert row.has_incomplete_key and not row.has_repeated_key
    assert row.key_record_count is None


@pytest.mark.parametrize("field,value", [
    ("base_passenger_fare", None), ("base_passenger_fare", -5.0),
    ("base_passenger_fare", float("nan")), ("tolls", None),
    ("tolls", -1.0), ("tolls", float("inf")),
])
def test_unresolved_money_is_preserved_and_keeps_demand(spark, field, value):
    row = inspect_trips(spark, [make_trip(**{field: value})])[0]
    assert row.is_demand_eligible and row.is_simulation_eligible
    assert not row.is_monetary_eligible
    name = "base_fare_usd" if field == "base_passenger_fare" else "tolls_usd"
    if value is None:
        assert row[name] is None
    elif value < 0:
        assert row[name] == value


def test_valid_fare_with_missing_tolls_can_support_fare_analysis(spark):
    row = inspect_trips(spark, [make_trip(tolls=None, tips=-1.0)])[0]
    assert row.is_fare_analysis_eligible and not row.is_monetary_eligible
    assert row.tips_usd == -1.0 and row.is_negative_tips_usd


def test_collision_key_is_exact_and_all_colliding_rows_survive(spark):
    assert COLLISION_KEY == [
        "service_license", "request_datetime", "pickup_datetime", "dropoff_datetime",
        "pickup_zone_id", "dropoff_zone_id",
    ]
    rows = inspect_trips(spark, [
        make_trip(), make_trip(dispatching_base_num="B00002", base_passenger_fare=-1.0),
        make_trip(hvfhs_license_num="HV0005"), make_trip(request_datetime=None),
        make_trip(request_datetime=None),
    ])
    assert len(rows) == 5
    repeated = [row for row in rows if row.has_repeated_key]
    assert len(repeated) == 2
    assert all(row.key_record_count == 2 for row in repeated)
    assert sum(row.has_incomplete_key for row in rows) == 2
    assert sum(row.is_accepted for row in rows) == 3
    assert sum(row.cleaning_status == "quarantined" for row in rows) == 2


def test_flags_are_nonnull_and_eligibility_can_overlap(spark):
    row = inspect_trips(spark, [make_trip()])[0]
    assert row.is_accepted and row.is_demand_eligible and row.is_simulation_eligible
    assert row.is_monetary_eligible and row.is_fare_analysis_eligible
    for name, value in row.asDict().items():
        if name.startswith(("is_", "has_")):
            assert isinstance(value, bool), name
    assert all(row[f"is_valid_{name}"] for name in MONEY_COLUMNS)


def test_empty_input_and_missing_columns(spark):
    empty = spark.createDataFrame([], schema=RAW_SCHEMA)
    assert clean_trips(empty, KNOWN_ZONE_IDS).count() == 0
    with pytest.raises(ValueError, match="Missing input columns"):
        clean_trips(empty.drop("trip_time"), KNOWN_ZONE_IDS)
    with pytest.raises(ValueError, match="unique"):
        clean_trips(empty, [161, 161])
