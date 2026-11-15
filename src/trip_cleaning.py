"""Step 1 of the ETL: flag raw trips and assign cleaning and analysis eligibility."""

from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F

MIN_TRIP_SECONDS = 60
MAX_TRIP_SECONDS = 3 * 60 * 60
MIN_SPEED_MPH = 1
MAX_SPEED_MPH = 80
SECONDS_PER_HOUR = 3600
MICROSECONDS_PER_SECOND = 1_000_000
RECOGNISED_UNMAPPED_ZONE_IDS = (264, 265)

COLUMN_NAMES = {
    "hvfhs_license_num": "service_license",
    "dispatching_base_num": "dispatching_base_number",
    "originating_base_num": "originating_base_number",
    "PULocationID": "pickup_zone_id",
    "DOLocationID": "dropoff_zone_id",
    "trip_time": "trip_seconds",
    "base_passenger_fare": "base_fare_usd",
    "tolls": "tolls_usd",
    "bcf": "black_car_fund_usd",
    "sales_tax": "sales_tax_usd",
    "congestion_surcharge": "congestion_surcharge_usd",
    "airport_fee": "airport_fee_usd",
    "tips": "tips_usd",
    "driver_pay": "driver_pay_usd",
}
COLLISION_KEY = [
    "service_license",
    "request_datetime",
    "pickup_datetime",
    "dropoff_datetime",
    "pickup_zone_id",
    "dropoff_zone_id",
]
MONEY_COLUMNS = [
    "base_fare_usd", "tolls_usd", "black_car_fund_usd", "sales_tax_usd",
    "congestion_surcharge_usd", "airport_fee_usd", "tips_usd", "driver_pay_usd",
]
CORE_COLUMNS = [
    "request_datetime", "pickup_datetime", "pickup_zone_id",
    "trip_seconds", "trip_miles",
]
QUALITY_FAILURE_FLAGS = [
    "has_missing_core_field", "has_nonfinite_trip_miles",
    "has_nonpositive_trip_seconds", "has_duration_outside_limits",
    "has_speed_outside_limits", "has_invalid_request_order",
]


def rename_trip_columns(trips: DataFrame) -> DataFrame:
    """Take raw TLC trips and return clearly named columns for the cleaning step."""
    required = list(COLUMN_NAMES) + [
        "request_datetime", "pickup_datetime", "dropoff_datetime", "trip_miles",
    ]
    missing = sorted(set(required) - set(trips.columns))
    if missing:
        raise ValueError(f"Missing input columns: {missing}")
    names = [COLUMN_NAMES.get(name, name) for name in trips.columns]
    if len(names) != len(set(names)):
        raise ValueError("Renaming would produce repeated column names")
    return trips.select([
        F.col(name).alias(COLUMN_NAMES.get(name, name)) for name in trips.columns
    ])


def flag_trip_quality(trips: DataFrame) -> DataFrame:
    """Take renamed trips and return quality flags and timing diagnostics, without dropping rows."""
    missing_core = F.lit(False)
    for name in CORE_COLUMNS:
        missing_core = missing_core | F.col(name).isNull()
    miles = F.col("trip_miles")
    seconds = F.col("trip_seconds")
    invalid_miles = F.isnan(miles) | (F.abs(miles) == float("inf"))
    trips = trips.withColumn("has_missing_core_field", missing_core)
    trips = trips.withColumn("has_nonfinite_trip_miles", F.coalesce(invalid_miles, F.lit(False)))
    trips = trips.withColumn(
        "speed_mph",
        F.when((seconds > 0) & ~invalid_miles, miles * SECONDS_PER_HOUR / seconds),
    )
    checks = {
        "has_nonpositive_trip_seconds": seconds <= 0,
        "has_duration_outside_limits": ~seconds.between(MIN_TRIP_SECONDS, MAX_TRIP_SECONDS),
        "has_speed_outside_limits": ~F.col("speed_mph").between(MIN_SPEED_MPH, MAX_SPEED_MPH),
        "has_invalid_request_order": F.col("request_datetime") >= F.col("pickup_datetime"),
        "has_missing_dropoff_datetime": F.col("dropoff_datetime").isNull(),
        "has_invalid_dropoff_order": F.col("dropoff_datetime") <= F.col("pickup_datetime"),
    }
    for name, condition in checks.items():
        trips = trips.withColumn(name, F.coalesce(condition, F.lit(False)))
    timestamp_seconds = (
        F.unix_micros(F.col("dropoff_datetime").cast("timestamp"))
        - F.unix_micros(F.col("pickup_datetime").cast("timestamp"))
    ) / MICROSECONDS_PER_SECOND
    trips = trips.withColumn("timestamp_trip_seconds", timestamp_seconds)
    trips = trips.withColumn("duration_difference_seconds", timestamp_seconds - seconds)
    return trips.withColumn(
        "has_timestamp_duration_disagreement",
        F.coalesce(F.col("duration_difference_seconds") != 0, F.lit(False)),
    )


def flag_key_collisions(trips: DataFrame) -> DataFrame:
    """Take renamed trips and flag repeated complete keys, preserving every record."""
    complete = F.lit(True)
    for name in COLLISION_KEY:
        complete = complete & F.col(name).isNotNull()
    complete = complete & (F.trim(F.col("service_license")) != "")
    trips = trips.withColumn("has_incomplete_key", ~complete)
    key_window = Window.partitionBy(*COLLISION_KEY)
    trips = trips.withColumn(
        "key_record_count", F.when(complete, F.count(F.lit(1)).over(key_window)),
    )
    return trips.withColumn(
        "has_repeated_key", F.coalesce(F.col("key_record_count") > 1, F.lit(False)),
    )


def flag_zone_eligibility(trips: DataFrame, known_zone_ids: list[int]) -> DataFrame:
    """Take renamed trips and lookup IDs, and return separate endpoint zone categories."""
    if not known_zone_ids or len(known_zone_ids) != len(set(known_zone_ids)):
        raise ValueError("Zone lookup must contain nonempty, unique LocationIDs")
    known = sorted(known_zone_ids)
    for endpoint in ["pickup", "dropoff"]:
        zone = F.col(f"{endpoint}_zone_id")
        category = (
            F.when(zone.isNull(), "missing")
            .when(~zone.isin(known), "unrecognised")
            .when(zone.isin(list(RECOGNISED_UNMAPPED_ZONE_IDS)), "recognised_unmapped")
            .otherwise("mapped")
        )
        trips = trips.withColumn(f"{endpoint}_zone_status", category)
    return trips


def flag_monetary_quality(trips: DataFrame) -> DataFrame:
    """Take renamed trips and flag unresolved money amounts without changing their values."""
    for name in MONEY_COLUMNS:
        amount = F.col(name)
        trips = trips.withColumn(f"is_missing_{name}", amount.isNull())
        trips = trips.withColumn(
            f"is_negative_{name}", F.coalesce(amount < 0, F.lit(False)),
        )
        trips = trips.withColumn(
            f"is_nonfinite_{name}",
            F.coalesce(F.isnan(amount) | (F.abs(amount) == float("inf")), F.lit(False)),
        )
        trips = trips.withColumn(
            f"is_valid_{name}",
            ~(F.col(f"is_missing_{name}") | F.col(f"is_negative_{name}")
              | F.col(f"is_nonfinite_{name}")),
        )
    return trips


def clean_trips(trips: DataFrame, known_zone_ids: list[int]) -> DataFrame:
    """Return every input trip with one cleaning partition and independent analysis flags."""
    trips = rename_trip_columns(trips)
    trips = flag_trip_quality(trips)
    trips = flag_zone_eligibility(trips, known_zone_ids)
    trips = flag_monetary_quality(trips)
    trips = flag_key_collisions(trips)
    passes_checks = F.lit(True)
    for name in QUALITY_FAILURE_FLAGS:
        passes_checks = passes_checks & ~F.col(name)
    trips = trips.withColumn("is_accepted", passes_checks)
    trips = trips.withColumn(
        "cleaning_status", F.when(passes_checks, "accepted").otherwise("quarantined"),
    )
    demand = passes_checks & (F.col("pickup_zone_status") == "mapped")
    simulation = (
        demand & (F.col("dropoff_zone_status") == "mapped")
        & ~F.col("has_missing_dropoff_datetime") & ~F.col("has_invalid_dropoff_order")
    )
    valid_fare = passes_checks & F.col("is_valid_base_fare_usd")
    return trips.withColumns({
        "is_demand_eligible": demand,
        "is_simulation_eligible": simulation,
        "is_fare_analysis_eligible": valid_fare,
        "is_monetary_eligible": valid_fare & F.col("is_valid_tolls_usd"),
    })
