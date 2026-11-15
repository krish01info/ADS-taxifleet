"""Phase 1 result-critical aggregation, coverage, joins and training-only references."""

from datetime import date, datetime, timezone
import re

from pyspark.sql import functions as F

from src.phase1_time import NYC_ZONE, add_event_time_keys, assign_analysis_period, build_verified_hour_grid

SPARSE_PAIR_THRESHOLD = 30
METRES_PER_MILE = 1609.344
MINUTES_PER_HOUR = 60
COUNT_COLUMNS = ["request_count_observed", "pickup_count_observed", "dropoff_count_observed",
                 "valid_fare_count", "valid_tolls_count", "monetary_trip_count",
                 "fare_per_minute_count", "passenger_measure_count"]
SUM_COLUMNS = ["valid_base_fare_sum_usd", "valid_tolls_sum_usd", "passenger_miles_sum",
               "passenger_minutes_sum", "fare_per_minute_sum"]
START = "2024-01-01"
END = "2025-02-01"


def check_unique_keys(table, keys):
    """Fail if a table has null or repeated keys before a join or handover."""
    missing = F.lit(False)
    for name in keys:
        missing = missing | F.col(name).isNull()
    if table.filter(missing).limit(1).count():
        raise ValueError(f"Null key in {keys}")
    if table.groupBy(*keys).count().filter(F.col("count") > 1).limit(1).count():
        raise ValueError(f"Repeated key in {keys}")


def join_reference_tables(facts, reference, keys):
    """Left-join a unique reference and assert that no fact rows multiply or disappear."""
    check_unique_keys(reference, keys)
    before = facts.count()
    joined = facts.join(reference, keys, "left")
    after = joined.count()
    if before != after:
        raise ValueError(f"Join cardinality changed: {before} -> {after}, keys {keys}")
    print(f"Join {keys}: {before} -> {after}", flush=True)
    return joined


def add_trip_time_fields(trips):
    """Add separate request/pickup/drop-off keys while preserving existing eligibility flags."""
    for event, source in [("request", "request_datetime"), ("pickup", "pickup_datetime"),
                          ("dropoff", "dropoff_datetime")]:
        trips = add_event_time_keys(trips, source, event)
    return trips


def event_eligibility(event):
    """Return the documented physical-activity eligibility for each event type."""
    if event in ("request", "pickup"):
        return F.col("is_demand_eligible")
    return (F.col("is_accepted") & (F.col("dropoff_zone_status") == "mapped")
            & ~F.col("has_missing_dropoff_datetime") & ~F.col("has_invalid_dropoff_order"))


def event_is_in_scope(event):
    """Select an event's civil-calendar study/drift interval independently of source filename."""
    day = F.col(f"{event}_date_nyc")
    return (day >= F.lit(START).cast("date")) & (day < F.lit(END).cast("date"))


def aggregate_zone_activity(trips):
    """Return additive zone-hour partials; averages are calculated later from true denominators."""
    pieces = []
    for event in ["request", "pickup", "dropoff"]:
        zone = "dropoff_zone_id" if event == "dropoff" else "pickup_zone_id"
        eligible = event_eligibility(event) & event_is_in_scope(event)
        resolved = trips.filter(eligible & (F.col(f"{event}_time_status") == "resolved"))
        groups = resolved.groupBy(F.col(zone).alias("zone_id"),
                                  F.col(f"{event}_hour_utc").alias("hour_start_utc"))
        expressions = [F.count(F.lit(1)).alias(f"{event}_count_observed")]
        if event == "request":
            fare = F.col("is_fare_analysis_eligible")
            toll = F.col("is_valid_tolls_usd")
            expressions += [
                F.count(F.when(fare, 1)).alias("valid_fare_count"),
                F.count(F.when(toll, 1)).alias("valid_tolls_count"),
                F.count(F.when(F.col("is_monetary_eligible"), 1)).alias("monetary_trip_count"),
                F.count(F.when(fare, 1)).alias("fare_per_minute_count"),
                F.count(F.lit(1)).alias("passenger_measure_count"),
                F.sum(F.when(fare, F.col("base_fare_usd"))).alias("valid_base_fare_sum_usd"),
                F.sum(F.when(toll, F.col("tolls_usd"))).alias("valid_tolls_sum_usd"),
                F.sum("trip_miles").alias("passenger_miles_sum"),
                F.sum(F.col("trip_seconds") / MINUTES_PER_HOUR).alias("passenger_minutes_sum"),
                F.sum(F.when(fare, F.col("base_fare_usd") / (F.col("trip_seconds") / MINUTES_PER_HOUR))).alias("fare_per_minute_sum"),
            ]
        piece = groups.agg(*expressions)
        for name in COUNT_COLUMNS + SUM_COLUMNS:
            if name not in piece.columns:
                piece = piece.withColumn(name, F.lit(None).cast("long" if name in COUNT_COLUMNS else "double"))
        pieces.append(piece.select("zone_id", "hour_start_utc", *COUNT_COLUMNS, *SUM_COLUMNS))
    combined = pieces[0]
    for piece in pieces[1:]:
        combined = combined.unionByName(piece)
    return combine_activity_partials(combined)


def combine_activity_partials(partials):
    """Pool additive counts/sums across source months, including legitimate boundary events."""
    expressions = [
        F.sum(name).alias(name) for name in COUNT_COLUMNS + SUM_COLUMNS
    ]
    if "source_month" in partials.columns:
        expressions.append(F.sort_array(F.collect_set("source_month")).alias("contributing_source_months"))
    combined = partials.groupBy("zone_id", "hour_start_utc").agg(*expressions)
    return combined.fillna(0, subset=COUNT_COLUMNS)


def unresolved_event_counts(trips):
    """Reconcile eligible events outside resolved hourly keys, retaining original civil times."""
    pieces = []
    for event in ["request", "pickup", "dropoff"]:
        zone = "dropoff_zone_id" if event == "dropoff" else "pickup_zone_id"
        rows = trips.filter(event_eligibility(event) & event_is_in_scope(event)
                            & (F.col(f"{event}_time_status") != "resolved"))
        pieces.append(rows.select(F.col(zone).alias("zone_id"), F.lit(event).alias("event_type"),
                                  F.col(f"{event}_time_status").alias("time_status"),
                                  F.col(f"{event}_date_nyc").alias("event_date_nyc"),
                                  F.col(f"{event}_datetime").cast("timestamp_ntz").alias("original_civil_time")))
    combined = pieces[0]
    for piece in pieces[1:]:
        combined = combined.unionByName(piece)
    return combined.groupBy("zone_id", "event_type", "time_status", "event_date_nyc",
                            "original_civil_time").count().withColumnRenamed("count", "event_count")


def reconcile_activity(trips, partials, unresolved):
    """Check each eligible event belongs to resolved, unresolved, or out-of-domain counts."""
    audit = {}
    for event in ["request", "pickup", "dropoff"]:
        counts = trips.agg(
            F.count(F.when(event_eligibility(event), 1)).alias("eligible"),
            F.count(F.when(event_eligibility(event) & ~event_is_in_scope(event), 1)).alias("out_of_scope"),
        ).first().asDict()
        counts["resolved_in_scope"] = partials.agg(F.sum(f"{event}_count_observed")).first()[0] or 0
        counts["unresolved_in_scope"] = unresolved.filter(F.col("event_type") == event).agg(F.sum("event_count")).first()[0] or 0
        if counts["eligible"] != sum(counts[name] for name in ["out_of_scope", "resolved_in_scope", "unresolved_in_scope"]):
            raise ValueError(f"{event} eligibility/time reconciliation failed: {counts}")
        audit[event] = counts
    return audit


def coverage_rows(available_months, is_sample=False):
    """Declare publisher-cohort coverage, with explicit adjacent-file boundary requirements."""
    available = set(available_months)
    result = []
    for year in [2024, 2025]:
        for month in range(1, 13 if year == 2024 else 2):
            current = f"{year}-{month:02d}"
            previous = f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"
            following = f"{year + 1}-01" if month == 12 else f"{year}-{month + 1:02d}"
            result.append((current, not is_sample and current in available and following in available,
                           not is_sample and current in available,
                           not is_sample and current in available and previous in available))
    return result


def build_zone_hour_table(spark, zones, partials, unresolved, available_months,
                          start_civil=START, end_civil=END, is_sample=False):
    """Build a unique grid with observed partials and null targets wherever coverage is incomplete."""
    grid = build_verified_hour_grid(spark, zones, start_civil, end_civil)
    grid = grid.withColumn("event_month", F.date_format("event_date_nyc", "yyyy-MM"))
    grid = grid.withColumn("period_label", assign_analysis_period(F.col("event_date_nyc")))
    grid = grid.withColumn("cohort", F.when(F.col("period_label") == "drift", "drift_2025_01").otherwise("study_2024"))
    coverage = spark.createDataFrame(coverage_rows(available_months, is_sample),
        "event_month string, request_source_coverage boolean, pickup_source_coverage boolean, dropoff_source_coverage boolean")
    table = join_reference_tables(grid, partials, ["zone_id", "hour_start_utc"])
    table = join_reference_tables(table, coverage, ["event_month"])
    table = table.fillna(0, subset=COUNT_COLUMNS)
    # Nonexistent source times have no real UTC hour: conservatively mark that zone/day incomplete.
    for event in ["request", "pickup", "dropoff"]:
        affected = unresolved.filter((F.col("event_type") == event) & (F.col("time_status") == "nonexistent"))
        affected = affected.groupBy("zone_id", "event_date_nyc").agg(F.sum("event_count").alias(f"{event}_nonexistent_count"))
        table = join_reference_tables(table, affected, ["zone_id", "event_date_nyc"])
        complete = (F.col(f"{event}_source_coverage") & ~F.col("is_ambiguous_civil_hour")
                    & F.col(f"{event}_nonexistent_count").isNull())
        table = table.withColumn(f"{event}_coverage_complete", complete)
        table = table.withColumn(f"{event}_count", F.when(complete, F.col(f"{event}_count_observed")))
    means = {
        "mean_base_fare_usd": ("valid_base_fare_sum_usd", "valid_fare_count"),
        "mean_tolls_usd": ("valid_tolls_sum_usd", "valid_tolls_count"),
        "mean_passenger_miles": ("passenger_miles_sum", "passenger_measure_count"),
        "mean_passenger_minutes": ("passenger_minutes_sum", "passenger_measure_count"),
        "mean_fare_per_trip_minute_usd": ("fare_per_minute_sum", "fare_per_minute_count"),
    }
    for name, (total, denominator) in means.items():
        table = table.withColumn(name, F.when(F.col(denominator) > 0, F.col(total) / F.col(denominator)))
    for name in SUM_COLUMNS:
        table = table.withColumn(name, F.when((F.col("request_count_observed") == 0)
                                             & F.col("request_coverage_complete"), F.lit(0.0)).otherwise(F.col(name)))
    table = table.withColumn("is_sample", F.lit(is_sample))
    table = table.withColumn("coverage_definition", F.lit(
        "Eligible completed trips in checked publisher cohorts; request look-ahead may remain censored beyond adjacent files"))
    check_unique_keys(table, ["zone_id", "hour_start_utc"])
    return join_reference_tables(table, zones.drop("geometry_wkb"), ["zone_id"])


def build_zone_pair_table(trips, zones, period_label, *, sparse_threshold=SPARSE_PAIR_THRESHOLD):
    """Calculate exact observed OD medians for one request-period, including explicit unsupported pairs."""
    if sparse_threshold < 1:
        raise ValueError("Sparse support threshold must be positive")
    if re.fullmatch(r"20\d{2}-\d{2}", period_label):
        period = F.date_format("request_datetime", "yyyy-MM") == period_label
    elif period_label == "study_2024":
        period = F.col("request_period").isin("train", "validation", "test")
    else:
        period = F.col("request_period") == period_label
    eligible = trips.filter(F.col("is_simulation_eligible") & period
                            & (F.col("request_time_status") == "resolved"))
    if period_label == "train":
        # Outcomes crossing the cutoff are not yet known at the end of training.
        eligible = eligible.filter(F.col("dropoff_datetime") < F.lit("2024-09-01").cast("timestamp_ntz"))
    observed = eligible.groupBy("pickup_zone_id", "dropoff_zone_id").agg(
        F.count(F.lit(1)).alias("trip_count"),
        F.median("trip_miles").alias("median_passenger_miles"),
        F.median(F.col("trip_seconds") / MINUTES_PER_HOUR).alias("median_trip_minutes"),
    )
    origins = zones.select(F.col("zone_id").alias("pickup_zone_id"),
                            F.col("centroid_x_metres").alias("origin_x"), F.col("centroid_y_metres").alias("origin_y"))
    destinations = zones.select(F.col("zone_id").alias("dropoff_zone_id"),
                                 F.col("centroid_x_metres").alias("destination_x"), F.col("centroid_y_metres").alias("destination_y"))
    result = join_reference_tables(origins.crossJoin(destinations), observed, ["pickup_zone_id", "dropoff_zone_id"])
    result = result.fillna(0, subset=["trip_count"])
    result = result.withColumn("period_label", F.lit(period_label))
    result = result.withColumn("support_status", F.when(F.col("trip_count") == 0, "unsupported")
                              .when(F.col("trip_count") < sparse_threshold, "sparse").otherwise("supported"))
    result = result.withColumn("support_threshold", F.lit(sparse_threshold))
    result = result.withColumn("straight_line_centroid_miles", F.sqrt(
        (F.col("origin_x") - F.col("destination_x")) ** 2
        + (F.col("origin_y") - F.col("destination_y")) ** 2) / METRES_PER_MILE)
    result = result.drop("origin_x", "origin_y", "destination_x", "destination_y")
    drift = period_label == "drift" or period_label == "2025-01"
    result = result.withColumn("cohort", F.lit("drift_2025_01" if drift else "study_2024"))
    result = result.withColumn("is_training_reference", F.lit(period_label == "train"))
    return result.withColumn("reference_period_end_nyc", F.lit("2024-09-01" if period_label == "train" else None).cast("date"))


def build_training_pair_reference(trips, zones):
    """Expose the train-only OD estimator; no other period can affect it."""
    return build_zone_pair_table(trips, zones, "train")


def build_provider_exclusion_audit(trips):
    """Return provider/zone/hour numerators and matched denominators for two recorded-time views."""
    prior_failure = F.lit(False)
    for flag in ["has_missing_core_field", "has_nonfinite_trip_miles", "has_nonpositive_trip_seconds",
                 "has_duration_outside_limits", "has_speed_outside_limits"]:
        prior_failure = prior_failure | F.col(flag)
    pieces = []
    for event in ["request", "pickup"]:
        rows = trips.filter(event_is_in_scope(event))
        pieces.append(rows.groupBy("service_license", "pickup_zone_id",
                                   F.lit(f"recorded_{event}_hour").alias("time_basis"),
                                   F.col(f"{event}_time_status").alias("time_status"),
                                   F.col(f"{event}_hour_utc").alias("hour_start_utc"),
                                   F.date_trunc("hour", F.col(f"{event}_datetime")).cast("timestamp_ntz").alias("recorded_civil_hour")).agg(
            F.count(F.lit(1)).alias("input_count"),
            F.count(F.when(F.col("has_invalid_request_order"), 1)).alias("request_order_flag_count"),
            F.count(F.when(F.col("has_invalid_request_order") & ~prior_failure, 1)).alias("sequential_request_order_exclusions"),
        ))
    return pieces[0].unionByName(pieces[1])


def combine_provider_audits(partials):
    """Pool exclusion counts before division, rather than averaging provider/month rates."""
    keys = ["service_license", "pickup_zone_id", "time_basis", "time_status", "hour_start_utc", "recorded_civil_hour"]
    result = partials.groupBy(*keys).agg(*[F.sum(name).alias(name) for name in
        ["input_count", "request_order_flag_count", "sequential_request_order_exclusions"]])
    return result.withColumn("sequential_exclusion_rate", F.col("sequential_request_order_exclusions") / F.col("input_count"))


def prepare_weather_hourly(spark, observations):
    """Choose the latest routine observation per station/hour, with deterministic tie handling."""
    from pyspark.sql import Window
    ranking = Window.partitionBy("station_id", "hour_start_utc").orderBy(
        F.col("observed_at_utc").desc(), F.col("source_row_sha256").asc())
    return observations.withColumn("_rank", F.row_number().over(ranking)).filter(F.col("_rank") == 1).drop("_rank")


def weather_schema():
    """Declare weather types even if an entire station file has a missing attribute."""
    schema = ("station_id string, observed_at_utc timestamp, hour_start_utc timestamp, report_type string, "
              "source_code string, source_time_convention string, product_version string, "
              "has_verified_publication_time boolean, present_weather_codes string, "
              "has_observed_rain boolean, has_observed_snow boolean, source_row_sha256 string")
    for name in ["temperature_c", "precipitation_previous_hour_mm", "wind_metres_per_second", "visibility_metres",
                 "relative_humidity_percent", "dew_point_c"]:
        schema += f", {name} double, {name}_quality string, {name}_raw string"
    return schema


def integrate_weather_calendar(table, weather, calendar):
    """Keep trip facts when reference data are missing; no station substitution or future weather features."""
    if weather is not None:
        for station_id, suffix in [("72505394728", ""), ("72503014732", "_laguardia"), ("74486094789", "_jfk")]:
            station = weather.filter(F.col("station_id") == station_id)
            station = station.select([F.col(name).alias(name + suffix if name != "hour_start_utc" else name)
                                      for name in station.columns])
            table = join_reference_tables(table, station, ["hour_start_utc"])
    else:
        for suffix in ["", "_laguardia", "_jfk"]:
            for field in spark_schema_fields(table.sparkSession, weather_schema()):
                if field.name != "hour_start_utc":
                    table = table.withColumn(field.name + suffix, F.lit(None).cast(field.dataType))
    table = table.withColumn("has_primary_weather", F.col("observed_at_utc").isNotNull())
    if calendar is not None:
        table = join_reference_tables(table, calendar, ["event_date_nyc"])
    else:
        table = table.withColumns({"month_nyc": F.month("event_date_nyc"),
            "weekday_nyc": F.pmod(F.dayofweek("event_date_nyc") + F.lit(5), F.lit(7)) + 1,
            "is_weekend": F.dayofweek("event_date_nyc").isin(1, 7),
            "is_federal_holiday": F.lit(None).cast("boolean"), "holiday_name": F.lit(None).cast("string"),
            "holiday_observed_date": F.lit(None).cast("date"), "holiday_statutory_date": F.lit(None).cast("date"),
            "holiday_source": F.lit(None).cast("string")})
    return table.withColumn("has_verified_holiday_calendar", F.col("is_federal_holiday").isNotNull())


def spark_schema_fields(spark, schema):
    """Parse an explicit schema without inferring types from missing observations."""
    return spark.createDataFrame([], schema=schema).schema.fields


def calendar_schema():
    """Return calendar types for the documented OPM transcription."""
    return ("event_date_nyc date, month_nyc int, weekday_nyc int, is_weekend boolean, "
            "is_federal_holiday boolean, holiday_name string, holiday_source string, "
            "holiday_observed_date date, holiday_statutory_date date")


def build_weather_station_checks(weather):
    """Compare observed Central Park weather with the two check stations; never substitute them."""
    primary = weather.filter(F.col("station_id") == "72505394728").select(
        "hour_start_utc", F.col("temperature_c").alias("primary_temperature_c"),
        F.col("wind_metres_per_second").alias("primary_wind"))
    result = []
    for station_id in ["72503014732", "74486094789"]:
        check = weather.filter(F.col("station_id") == station_id).select(
            "hour_start_utc", "temperature_c", "wind_metres_per_second")
        matched = primary.join(check, "hour_start_utc", "inner")
        result.append(matched.agg(
            F.count(F.lit(1)).alias("paired_observation_hours"),
            F.count(F.when(F.col("primary_temperature_c").isNotNull() & F.col("temperature_c").isNotNull(), 1)).alias("valid_temperature_pairs"),
            F.avg(F.abs(F.col("primary_temperature_c") - F.col("temperature_c"))).alias("mean_absolute_temperature_difference_c"),
            F.count(F.when(F.col("primary_wind").isNotNull() & F.col("wind_metres_per_second").isNotNull(), 1)).alias("valid_wind_pairs"),
            F.avg(F.abs(F.col("primary_wind") - F.col("wind_metres_per_second"))).alias("mean_absolute_wind_difference_metres_per_second"),
        ).withColumn("comparison_station_id", F.lit(station_id)))
    return result[0].unionByName(result[1])


def affected_hour_periods(start_civil, end_civil):
    """Return real UTC bounds as metadata; fixed EST is not substituted for civil dates."""
    return {name: datetime.combine(date.fromisoformat(value), datetime.min.time()).replace(tzinfo=NYC_ZONE)
            .astimezone(timezone.utc).isoformat() for name, value in [("start_utc", start_civil), ("end_utc", end_civil)]}
