"""Phase 1 time keys: preserve civil timestamps and expose unresolved DST times."""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

NYC_ZONE = ZoneInfo("America/New_York")
UTC = timezone.utc
EST = timezone(timedelta(hours=-5))
PERIOD_BOUNDS = [
    ("train", "2024-01-01", "2024-09-01"),
    ("validation", "2024-09-01", "2024-11-01"),
    ("test", "2024-11-01", "2025-01-01"),
    ("drift", "2025-01-01", "2025-02-01"),
]


def classify_local_time(value):
    """Return status and UTC for a naive NYC time, without guessing a DST fold."""
    if value is None:
        return "missing", None
    if value.tzinfo is not None:
        raise ValueError("TLC input must be an offset-free civil timestamp")
    candidates = set()
    for fold in (0, 1):
        instant = value.replace(tzinfo=NYC_ZONE, fold=fold).astimezone(UTC)
        if instant.astimezone(NYC_ZONE).replace(tzinfo=None) == value:
            candidates.add(instant)
    if not candidates:
        return "nonexistent", None
    if len(candidates) == 2:
        return "ambiguous", None
    return "resolved", candidates.pop()


@lru_cache(maxsize=8)
def local_time_exceptions(first_year=2023, last_year=2026):
    """Find NYC's exceptional hourly intervals from the installed timezone database."""
    exceptions = []
    current = datetime(first_year, 1, 1)
    end = datetime(last_year + 1, 1, 1)
    while current < end:
        status, _ = classify_local_time(current)
        if status != "resolved":
            exceptions.append((current, current + timedelta(hours=1), status))
        current += timedelta(hours=1)
    return tuple(exceptions)


def assign_analysis_period(date_column):
    """Return the chronological period from a New York civil event date."""
    period = F.lit("out_of_scope")
    for name, start, end in PERIOD_BOUNDS:
        period = F.when((date_column >= F.lit(start).cast("date"))
                        & (date_column < F.lit(end).cast("date")), name).otherwise(period)
    return period


def add_event_time_keys(trips: DataFrame, source_column: str, prefix: str) -> DataFrame:
    """Add resolved UTC/EST keys and civil calendar fields; never modify cleaning flags."""
    if trips.sparkSession.conf.get("spark.sql.session.timeZone") != "UTC":
        raise ValueError("Use a UTC Spark session for unambiguous timestamp arithmetic")
    civil = F.col(source_column).cast("timestamp_ntz")
    status = F.when(civil.isNull(), "missing").otherwise("resolved")
    for start, end, label in local_time_exceptions():
        status = F.when((civil >= F.lit(start.isoformat()).cast("timestamp_ntz"))
                        & (civil < F.lit(end.isoformat()).cast("timestamp_ntz")), label).otherwise(status)
    trips = trips.withColumn(f"{prefix}_time_status", status)
    resolved = F.col(f"{prefix}_time_status") == "resolved"
    utc = F.when(resolved, F.to_utc_timestamp(civil.cast("timestamp"), "America/New_York"))
    trips = trips.withColumn(f"{prefix}_utc", utc)
    trips = trips.withColumn(f"{prefix}_hour_utc", F.date_trunc("hour", utc))
    trips = trips.withColumn(f"{prefix}_hour_est", (F.col(f"{prefix}_hour_utc")
                                                   - F.expr("INTERVAL 5 HOURS")).cast("timestamp_ntz"))
    trips = trips.withColumn(f"{prefix}_date_nyc", F.to_date(civil))
    return trips.withColumn(f"{prefix}_period", assign_analysis_period(F.to_date(civil)))


def build_verified_hour_grid(spark, zones, start_civil, end_civil):
    """Build unique UTC hours for a civil interval; callers attach measure-specific coverage."""
    start = datetime.fromisoformat(start_civil).replace(tzinfo=NYC_ZONE).astimezone(UTC)
    end = datetime.fromisoformat(end_civil).replace(tzinfo=NYC_ZONE).astimezone(UTC)
    hours = []
    current = start
    while current < end:
        local = current.astimezone(NYC_ZONE)
        wall = local.replace(tzinfo=None)
        status, _ = classify_local_time(wall)
        hours.append((current, current.astimezone(EST).replace(tzinfo=None),
                      local.date(), local.hour, int(local.utcoffset().total_seconds() / 60),
                      bool(local.dst()), status == "ambiguous"))
        current += timedelta(hours=1)
    schema = ("hour_start_utc timestamp, hour_start_est timestamp_ntz, event_date_nyc date, "
              "hour_nyc int, utc_offset_minutes int, is_dst boolean, is_ambiguous_civil_hour boolean")
    timeline = spark.createDataFrame(hours, schema=schema)
    return zones.select("zone_id").crossJoin(timeline)
