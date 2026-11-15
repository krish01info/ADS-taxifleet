"""Run the approved Phase 1 acquisition and notebook validation batch from a normal terminal."""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import subprocess
import time
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.phase1_sources import (  # noqa: E402
    build_zone_metadata,
    download_missing_files,
    expected_sources,
    hash_file,
    inventory_sources,
    prepare_calendar,
    prepare_weather,
    write_json_new,
)


def acquire_sources(root, acquire=False):
    """Save one consolidated source list and inventory; optionally acquire missing files."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    folder = root / "data/audits/phase1" / f"acquisition-{run_id}"
    records = expected_sources(root)
    inventory = inventory_sources(records)
    missing = [record for record in inventory if record["status"] != "checked"]
    folder.mkdir(parents=True, exist_ok=False)
    with (folder / "missing_downloads.txt").open("x") as stream:
        for record in missing:
            stream.write(f"{record['source_id']} ({record.get('role', 'reference')})\n{record['url']}\n{record['path']}\n\n")
    print("Consolidated download list:", folder / "missing_downloads.txt", flush=True)
    if acquire:
        inventory = download_missing_files(records, Path.home() / "Downloads")
    write_json_new(folder / "inventory.json", inventory)
    os.environ["ADS_PHASE1_INVENTORY_PATH"] = str(folder / "inventory.json")
    print("Source status:", {status: sum(row["status"] == status for row in inventory)
                             for status in sorted({row["status"] for row in inventory})}, flush=True)
    return inventory


def note_progress(root, stage, status, **details):
    """Append a compact local checkpoint note after a durable phase milestone."""
    path = root / "data/inspection/phase1_progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at_utc": datetime.now(timezone.utc).isoformat(), "stage": stage,
           "status": status, **details}
    with path.open("a") as stream:
        stream.write(json.dumps(row, default=str) + "\n")


def runtime_versions():
    """Record the actual runtime rather than the shell prompt's Python label."""
    java = subprocess.run(["java", "-version"], capture_output=True, text=True, check=True)
    versions = {"python": platform.python_version(), "java": java.stderr.splitlines()[0]}
    for name in ["pyspark", "py4j", "pyarrow", "pyshp", "shapely", "pyproj", "ruff", "pytest", "nbclient"]:
        versions[name] = metadata.version(name)
    if sys.version_info[:2] != (3, 11) or versions["pyspark"] != "4.0.1":
        raise RuntimeError("Use the validated Python 3.11 / Spark 4.0.1 environment")
    if not any(f'version "{major}.' in versions["java"] for major in [17, 21]):
        raise RuntimeError("Spark 4.0.1 requires supported Java 17 or 21")
    return versions


def start_spark():
    """Start bounded local Spark; use a normal terminal when sandbox sockets are blocked."""
    from pyspark.sql import SparkSession
    os.environ["TZ"] = "UTC"
    time.tzset()
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ.setdefault("SPARK_LOCAL_IP", "127.0.0.1")
    spark = (SparkSession.builder.master("local[2]").appName("ADS-Phase1-integration")
             .config("spark.driver.memory", "3g").config("spark.driver.maxResultSize", "128m")
             .config("spark.driver.bindAddress", "127.0.0.1").config("spark.ui.enabled", "false")
             .config("spark.sql.session.timeZone", "UTC").config("spark.sql.shuffle.partitions", "48")
             .config("spark.sql.parquet.outputTimestampType", "TIMESTAMP_MICROS")
             .config("spark.sql.ansi.enabled", "true").getOrCreate())
    spark.sparkContext.setLogLevel("WARN")
    return spark


def parquet_rows(path):
    """Count saved rows from Parquet footers without loading trip data into Python RAM."""
    import pyarrow.parquet as parquet
    return sum(parquet.ParquetFile(file).metadata.num_rows for file in Path(path).rglob("*.parquet"))


def verified_cleaning_cache(root, record):
    """Reuse compatible completed cleaning audits, including the approved January checkpoint."""
    cleaner_hash = hash_file(root / "src/trip_cleaning.py")
    checkpoint = subprocess.check_output(["git", "show", "da8bf65:src/trip_cleaning.py"], cwd=root)
    checkpoint_hash = hashlib.sha256(checkpoint).hexdigest()
    candidates = sorted((root / "data/audits/trip_cleaning").glob("*.json"))
    for path in candidates:
        audit = json.loads(path.read_text())
        if audit.get("status") != "complete" or audit.get("expected_rows") != record["rows"]:
            continue
        source_key = str(Path(record["path"]).relative_to(root))
        if audit.get("input_sha256", {}).get(source_key) != record["sha256"]:
            continue
        saved_hash = audit.get("cleaner_source_sha256")
        january_checkpoint = audit.get("scope") == "january-2024" and cleaner_hash == checkpoint_hash
        if saved_hash != cleaner_hash and not january_checkpoint:
            continue
        output_path = root / audit["output_path"]
        if not (output_path / "_SUCCESS").exists() or parquet_rows(output_path) != record["rows"]:
            continue
        return {"source_month": record["source_month"], "source_sha256": record["sha256"],
                "output_path": str(output_path), "audit_path": str(path), "rows": record["rows"],
                "cleaner_source_sha256": cleaner_hash, "reused": True}
    return None


def clean_available_months(root, inventory, spark, versions, *, sample=False):
    """Process checked sources sequentially; never rerun a compatible completed month."""
    import csv
    from pyspark.sql import functions as F
    from src.trip_audit import run_cleaning_scope
    with (root / "data/raw/zones/taxi_zone_lookup.csv").open(newline="") as stream:
        known_zone_ids = [int(row["LocationID"]) for row in csv.DictReader(stream)]
    result = []
    sources = [row for row in inventory if row["kind"] == "trips" and row["status"] == "checked"]
    for record in sources:
        if sample and record["source_month"] != "2024-01":
            continue
        raw = spark.read.parquet(record["path"])
        source_file_rows = record["rows"]
        if record.get("role") == "boundary_guard":
            # Full guard files remain raw. Only relevant-event rows need cleaning.
            relevant = F.lit(False)
            for event in ["request_datetime", "pickup_datetime", "dropoff_datetime"]:
                relevant = relevant | ((F.col(event) >= F.lit("2024-01-01").cast("timestamp_ntz"))
                                       & (F.col(event) < F.lit("2025-02-01").cast("timestamp_ntz")))
            raw = raw.filter(relevant)
            record = {**record, "rows": raw.count()}
        cached = verified_cleaning_cache(root, record)
        if cached:
            cached["source_file_rows"] = source_file_rows
            cached["role"] = record.get("role", "analysis")
            result.append(cached)
            print("Reusing validated cleaning:", record["source_month"], flush=True)
            note_progress(root, "trip_cleaning", "reused", source_month=record["source_month"],
                          rows=cached["rows"], audit=cached["audit_path"])
            continue
        count = min(10_000, record["rows"]) if sample else record["rows"]
        if sample:
            raw = raw.limit(count)
        audit = run_cleaning_scope(raw, count, f"{'sample-' if sample else ''}{record['source_month']}",
            project_root=root, known_zone_ids=known_zone_ids, versions=versions,
            input_hashes={str(Path(record["path"]).relative_to(root)): record["sha256"]})
        result.append({"source_month": record["source_month"], "source_sha256": record["sha256"],
                       "output_path": str(root / audit["output_path"]), "rows": count,
                       "audit_path": str(root / "data/audits/trip_cleaning" / (Path(audit["output_path"]).parent.name + ".json")),
                       "source_file_rows": source_file_rows, "role": record.get("role", "analysis"),
                       "cleaner_source_sha256": hash_file(root / "src/trip_cleaning.py"), "reused": False})
        note_progress(root, "trip_cleaning", "complete", source_month=record["source_month"],
                      rows=count, audit=result[-1]["audit_path"])
    if not result:
        raise RuntimeError("No checked trip source is available; real-data execution cannot proceed")
    return result


def save_table(table, path, partition_columns=None):
    """Save a fresh compact Parquet table, then verify the physical row total."""
    from pyspark import StorageLevel
    table = table.persist(StorageLevel.DISK_ONLY)
    try:
        expected = table.count()
        writer = table.write.mode("errorifexists").option("compression", "zstd")
        if partition_columns:
            writer = writer.partitionBy(*partition_columns)
        writer.parquet(str(path))
        actual = parquet_rows(path)
        if actual != expected:
            raise ValueError(f"Saved-output row mismatch for {path}: {expected} != {actual}")
        saved_schema = table.sparkSession.read.parquet(str(path)).schema.jsonValue()
        return {"path": str(path), "rows": actual, "schema": saved_schema}
    finally:
        table.unpersist()


def load_reference_sources(root, inventory, spark, output):
    """Prepare independent authentic sources, retaining explicit blockers instead of invented values."""
    from pyspark.sql import functions as F
    from src.phase1_tables import build_weather_station_checks, calendar_schema, prepare_weather_hourly, weather_schema
    zones, edges = build_zone_metadata(root / "data/raw/zones/taxi_zone_lookup.csv",
                                      root / "data/raw/zones/taxi_zones/taxi_zones.shp")
    zone_table = spark.createDataFrame(zones)
    edge_schema = ("zone_id long, neighbour_zone_id long, shares_boundary boolean, "
                   "touches_at_point boolean, shared_boundary_metres double")
    edge_table = spark.createDataFrame(edges, schema=edge_schema)
    references = {"zones": save_table(zone_table, output / "zones"),
                  "zone_neighbours": save_table(edge_table, output / "zone_neighbours")}
    weather_rows, proofs, blockers = [], {}, []
    for record in inventory:
        if record["kind"] != "weather":
            continue
        if record["status"] != "checked":
            blockers.append(f"Missing weather: {record['source_id']}")
            continue
        try:
            rows, proof = prepare_weather(record["path"], record["station_id"])
            weather_rows.extend(rows)
            proofs[record["source_id"]] = proof
        except ValueError as error:
            blockers.append(f"Unverified weather {record['source_id']}: {error}")
    weather = None
    if weather_rows:
        observations = spark.createDataFrame(weather_rows, schema=weather_schema())
        observations = observations.filter((F.col("observed_at_utc") >= F.lit("2024-01-01T05:00:00Z").cast("timestamp"))
                                            & (F.col("observed_at_utc") < F.lit("2025-02-01T05:00:00Z").cast("timestamp")))
        references["weather_observations"] = save_table(observations, output / "weather_observations")
        weather = prepare_weather_hourly(spark, observations)
        references["weather_station_hour"] = save_table(weather, output / "weather_station_hour")
        weather = spark.read.parquet(str(output / "weather_station_hour"))
        references["weather_station_checks"] = save_table(build_weather_station_checks(weather), output / "weather_station_checks")
    calendar, calendar_rows = None, []
    for start, end in [(date(2024, 1, 1), date(2025, 1, 1)), (date(2025, 1, 1), date(2025, 2, 1))]:
        try:
            calendar_rows.extend(prepare_calendar(root / "data/raw/calendar/opm_federal_holidays.html", start, end))
        except (OSError, ValueError) as error:
            blockers.append(f"Unverified OPM calendar {start.year}: {error}")
    if calendar_rows:
        calendar = spark.createDataFrame(calendar_rows, schema=calendar_schema())
        references["calendar"] = save_table(calendar, output / "calendar")
    write_json_new(output / "reference_source_evidence.json", {"weather": proofs, "blockers": blockers,
        "raw_routine_weather_rows": len(weather_rows),
        "retained_requested_period_weather_rows": references.get("weather_observations", {}).get("rows", 0),
        "spatial": {"zones": len(zones), "directed_edges": len(edges), "crs": zones[0]["source_crs"]}})
    return zone_table, weather, calendar, references, blockers


def find_partial_cache(root, signature):
    """Find previously verified month integration partials with matching inputs and logic."""
    for path in sorted((root / "data/audits/phase1").glob("month-*.json")):
        record = json.loads(path.read_text())
        if record.get("signature") != signature:
            continue
        if all(Path(table["path"]).exists() and parquet_rows(table["path"]) == table["rows"]
               for table in record["tables"].values()):
            return record
    return None


def integrate_month(root, month, spark, *, sample=False):
    """Write auditable per-source partials, retaining events that cross a source-file boundary."""
    from pyspark import StorageLevel
    from src.phase1_tables import (
        add_trip_time_fields, aggregate_zone_activity, build_provider_exclusion_audit,
        reconcile_activity, unresolved_event_counts,
    )
    logic = hash_file(root / "src/phase1_tables.py") + hash_file(root / "src/phase1_time.py")
    signature = hashlib.sha256((month["source_sha256"] + month["cleaner_source_sha256"] + logic + str(sample)).encode()).hexdigest()
    cached = find_partial_cache(root, signature)
    if cached:
        print("Reusing integration partials:", month["source_month"], flush=True)
        note_progress(root, "monthly_integration", "reused", source_month=month["source_month"],
                      audit=next((str(path) for path in (root / "data/audits/phase1").glob("month-*.json")
                                  if json.loads(path.read_text()).get("signature") == signature), None))
        return cached
    run_id = f"month-{month['source_month']}-{'sample-' if sample else ''}{uuid.uuid4().hex[:10]}"
    folder = root / "data/processed/phase1" / run_id
    trips = spark.read.parquet(month["output_path"])
    if sample:
        trips = trips.limit(10_000)
    # Select before caching: do not retain a year's wide annotated trips in laptop RAM.
    names = ["service_license", "request_datetime", "pickup_datetime", "dropoff_datetime",
             "pickup_zone_id", "dropoff_zone_id", "trip_miles", "trip_seconds", "base_fare_usd", "tolls_usd",
             "is_accepted", "is_demand_eligible", "is_simulation_eligible", "is_fare_analysis_eligible",
             "is_monetary_eligible", "is_valid_tolls_usd", "pickup_zone_status", "dropoff_zone_status",
             "has_missing_dropoff_datetime", "has_invalid_dropoff_order", "has_missing_core_field",
             "has_nonfinite_trip_miles", "has_nonpositive_trip_seconds", "has_duration_outside_limits",
             "has_speed_outside_limits", "has_invalid_request_order"]
    timed = add_trip_time_fields(trips.select(*names)).persist(StorageLevel.DISK_ONLY)
    try:
        partials = aggregate_zone_activity(timed)
        unresolved = unresolved_event_counts(timed)
        reconciled = reconcile_activity(timed, partials, unresolved)
        from pyspark.sql import functions as F
        mismatches = [row.asDict() for row in timed.filter(
            F.date_format("pickup_datetime", "yyyy-MM") != month["source_month"]
        ).groupBy(F.date_format("pickup_datetime", "yyyy-MM").alias("pickup_month")).count().collect()]
        partials = partials.withColumn("source_month", F.lit(month["source_month"]))
        tables = {"activity": save_table(partials, folder / "activity"),
                  "unresolved": save_table(unresolved, folder / "unresolved"),
                  "provider_exclusions": save_table(build_provider_exclusion_audit(timed), folder / "provider_exclusions")}
    finally:
        timed.unpersist()
    record = {"signature": signature, "source_month": month["source_month"], "is_sample": sample,
              "source_sha256": month["source_sha256"], "tables": tables, "event_reconciliation": reconciled,
              "pickup_months_outside_source": mismatches}
    write_json_new(root / "data/audits/phase1" / f"{run_id}.json", record)
    note_progress(root, "monthly_integration", "complete", source_month=month["source_month"],
                  audit=f"data/audits/phase1/{run_id}.json")
    print("Integrated source month:", month["source_month"], json.dumps(reconciled), flush=True)
    return record


def union_saved_tables(spark, partials, name):
    """Read only compact month partials when combining their additive measures."""
    return spark.read.parquet(*[record["tables"][name]["path"] for record in partials])


def cross_file_collision_audit(spark, months, partials):
    """Audit cross-source collisions, avoiding a full-key shuffle when pickup-month ownership proves zero."""
    from pyspark.sql import functions as F
    from src.trip_cleaning import COLLISION_KEY
    anomalous_months = sorted({item["pickup_month"] for record in partials
                               for item in record["pickup_months_outside_source"]})
    schema = ("service_license string, request_datetime timestamp_ntz, pickup_datetime timestamp_ntz, "
              "dropoff_datetime timestamp_ntz, pickup_zone_id long, dropoff_zone_id long, "
              "record_count long, source_month_count long, source_months array<string>")
    if not anomalous_months:
        return spark.createDataFrame([], schema), {
            "method": "Verified each processed row's pickup month equals its source month. The exact key contains pickup_datetime, so a key cannot span different source months.",
            "cross_file_groups": 0, "scope": "checked processed source files only"}
    candidates = None
    for month in months:
        table = spark.read.parquet(month["output_path"]).select(*COLLISION_KEY)
        complete = F.trim(F.col("service_license")) != ""
        for name in COLLISION_KEY:
            complete = complete & F.col(name).isNotNull()
        table = table.filter(complete & F.date_format("pickup_datetime", "yyyy-MM").isin(anomalous_months))
        table = table.withColumn("source_month", F.lit(month["source_month"]))
        candidates = table if candidates is None else candidates.unionByName(table)
    result = candidates.groupBy(*COLLISION_KEY).agg(F.count(F.lit(1)).alias("record_count"),
        F.countDistinct("source_month").alias("source_month_count"),
        F.sort_array(F.collect_set("source_month")).alias("source_months")).filter(F.col("source_month_count") > 1)
    return result, {"method": "Exact key comparison for anomalous pickup-month ownership; no records deleted",
                    "scope": "checked processed source files only", "candidate_pickup_months": anomalous_months}


def build_dictionary(table_records):
    """Document schemas, units, roles and missing-value contracts without padding observations."""
    dictionary = {}
    from src.trip_cleaning import COLUMN_NAMES
    original_names = {value: key for key, value in COLUMN_NAMES.items()}
    for table_name, record in table_records.items():
        fields = []
        for field in record["schema"]["fields"]:
            name = field["name"]
            base = name.removesuffix("_laguardia").removesuffix("_jfk")
            units = "dimensionless/category"
            if "usd" in name:
                units = "USD"
            elif "square_metres" in name:
                units = "square metres"
            elif "minutes" in name:
                units = "minutes"
            elif "seconds" in name:
                units = "seconds"
            elif "miles" in name:
                units = "miles"
            elif "metres_per_second" in name:
                units = "metres/second"
            elif "metres" in name:
                units = "metres"
            elif "count" in name:
                units = "records/events"
            elif name == "speed_mph":
                units = "miles/hour"
            elif base in ["temperature_c", "dew_point_c"]:
                units = "degrees Celsius"
            elif base == "precipitation_previous_hour_mm":
                units = "millimetres over the observation's preceding hour, not a clock-hour sum"
            elif base == "relative_humidity_percent":
                units = "percent"
            elif "longitude" in name or "latitude" in name:
                units = "decimal degrees (WGS84)"
            elif "timestamp" in str(field["type"]):
                units = "UTC instant" if "utc" in name else "civil or fixed-EST wall time, as named"
            elif str(field["type"]) == "date":
                units = "New York civil date"
            if base.endswith("_quality"):
                units = "source parsing/quality category"
            elif base.endswith("_raw"):
                units = {"temperature_c_raw": "source degrees Fahrenheit", "dew_point_c_raw": "source degrees Fahrenheit",
                         "precipitation_previous_hour_mm_raw": "source inches/trace code", "wind_metres_per_second_raw": "source mph",
                         "visibility_metres_raw": "source miles", "relative_humidity_percent_raw": "source percent"}.get(base, "original source encoding")
            if name in ["mean_fare_per_trip_minute_usd", "fare_per_minute_sum"]:
                units = "USD per passenger minute (per-trip ratio; summed only to pool its mean)"
            role = "descriptive/observed outcome; not automatically a predictor"
            origin = "transparent derivation from recorded source data"
            source_column = None
            if name in record.get("source_columns", []):
                origin, source_column = "recorded TLC field", original_names.get(name, name)
            if any(word in name for word in ["coverage", "quality", "status", "is_sample", "sha256", "source", "publication", "eligible", "is_valid", "is_missing", "is_negative", "is_nonfinite", "has_", "is_accepted", "key_record_count"]):
                role = "quality/provenance metadata"
                origin = "quality/provenance metadata, not an independent observed variable"
            elif name in ["event_date_nyc", "hour_nyc", "month_nyc", "weekday_nyc", "is_weekend", "is_federal_holiday", "holiday_name", "zone_id", "borough", "zone_name"]:
                role = "calendar/static attribute; respect source availability"
            if base.startswith("has_observed_"):
                role = "weather-code derivation; descriptive, publication latency unverified"
            definition = f"{name.replace('_', ' ')}; derived by the named Phase 1 function, not an additional independent observation"
            if name.endswith("_count_observed"):
                definition = "Resolved eligible completed-trip events found in checked input cohorts, including partial coverage"
            elif name in ["request_count", "pickup_count", "dropoff_count"]:
                definition = "Eligible event count only when that measure's coverage is complete; otherwise null"
            elif name == "mean_base_fare_usd":
                definition = "Valid base fare sum / valid_fare_count within demand-eligible request-zone/hour trips"
            elif name == "mean_fare_per_trip_minute_usd":
                definition = "Mean of valid per-trip base-fare / passenger minutes, not a ratio of pooled sums"
            elif name.startswith("median_"):
                definition = "Exact pooled eligible-trip OD median for the labelled request period; unsupported pairs are null"
            elif name == "straight_line_centroid_miles":
                definition = "Projected centroid Euclidean distance converted to miles; not observed road miles or an imputed median"
            elif base.startswith("precipitation_previous_hour_mm"):
                definition = "Latest routine observation's preceding-hour accumulation; overlapping accumulations are never added"
            elif base.endswith("_raw"):
                definition = "Unchanged source text, including qualifiers/missing/trace codes; inspect units before use"
            elif name in ["trip_miles", "trip_seconds", "base_fare_usd", "tolls_usd"]:
                definition = "Original recorded TLC passenger-trip value, renamed with explicit units; never filled or clipped"
            elif name == "weekday_nyc":
                definition = "ISO weekday of the New York civil date: Monday=1 through Sunday=7"
            elif name == "hour_start_est":
                definition = "Fixed UTC-05:00 standard-time wall-clock representation, not New York civil summer time"
            elif name == "hour_nyc":
                definition = "America/New_York civil hour 0-23; fall-back repetitions have distinct UTC keys"
            fields.append({**field, "definition": definition, "units": units, "feature_role": role,
                           "origin": origin, "source_column": source_column,
                           "null_policy": "Unknown/unavailable/unresolved stays null; count zeros require verified coverage"})
        dictionary[table_name] = {"path": record["path"], "rows": record["rows"], "fields": fields}
    return dictionary


def export_handover(root, spark, output, table_records, report, *, sample):
    """Export small real-data samples and explicit loading/coverage notes before claiming readiness."""
    folder = root / "data/handover" / output.name
    folder.mkdir(parents=True, exist_ok=False)
    dictionary = build_dictionary(table_records)
    write_json_new(folder / "data_dictionary.json", dictionary)
    write_json_new(folder / "run_summary.json", report)
    for name, record in table_records.items():
        if name in ["zone_hour", "zones", "zone_neighbours", "training_zone_pair_reference"]:
            table = spark.read.parquet(record["path"]).limit(1000)
            save_table(table, folder / "samples" / name)
    text = f'''# Phase 1 handover — {report['status']}

This run is {'a bounded development SAMPLE, not full-period data' if sample else 'a full AVAILABLE-input run; inspect actual coverage before use'}.
Original cleaning checkpoint: da8bf65. Cleaner unchanged; source-month guards do not define analytical periods.

## Loading
```python
spark.conf.set("spark.sql.session.timeZone", "UTC")
zone_hour = spark.read.parquet({table_records['zone_hour']['path']!r})
reference = spark.read.parquet({table_records['training_zone_pair_reference']['path']!r})
```
The companion dictionary records exact paths/types. `samples/` contains at most 1,000 real rows per table,
with the same saved schema and this explicit sample-only directory/manifest; never present these as full-period outputs.

## Required contracts
- Key: zone_id + hour_start_utc; UTC is the instant, fixed EST is UTC-05:00, not civil summer time.
- Civil calendar: America/New_York. Train Jan-Aug 2024; validation Sep-Oct; test Nov-Dec; January 2025 drift only.
- Training references exclude outcomes crossing 1 September; `reference_period_end_nyc` is the exclusive training cutoff, not a verified public-data publication timestamp. Do not use them as past-known features for earlier training dates.
- Request counts represent eligible completed-trip requests, not unserved/all real requests. Verified publisher cohorts still have possible long-wait boundary censoring beyond adjacent files.
- `*_count_observed` contains partial resolved observations. Target `request_count`/pickup/dropoff counts are null when their coverage is incomplete. Missing months never become zeros.
- Ambiguous/nonexistent civil timestamps stay auditable in unresolved-event outputs; core flags are unchanged.
- Monetary means use their valid-value count; unresolved money is not zero. Per-trip fare/minute is not pooled fare divided by pooled duration.
- Weather is retrospective. Contemporaneous weather and trip outcomes are not automatically forecast-time features. Publication latency is unverified.
- OD sparse threshold 30 is descriptive, not a confidence guarantee; sparse medians stay, unsupported medians are null. Straight-line centroid miles are not observed driving miles.
- Exclusion rates differ by provider. Zone/hour diagnostics use both recorded request and pickup time, without guessed repairs.
- The original report's undocumented collision rate and full-year audit claims are still unverified.

## Actual coverage / blockers
{json.dumps(report.get('blockers', []), indent=2)}

No forecasting model, simulator, launch-area optimisation, commit or push is part of this run.
'''
    (folder / "README.md").write_text(text)
    lines = ["# Data preparation — measured draft", "", f"Run status: **{report['status']}**.", "",
             f"This is {'a bounded SAMPLE, not full-period evidence' if sample else 'an available-input execution, with completeness determined by the listed blockers'}.",
             "", "NYC TLC HVFHV trips are a completed-trip demand proxy, not observations of unserved requests.",
             "The validated cleaner was retained: duration 60–10,800 seconds, speed 1–80 mph, request strictly before pickup.",
             "Repeated six-field keys were flagged without deletion; unresolved money was retained without zero filling.",
             "", "| Measured output | Rows |", "|---|---:|"]
    for name in ["zone_hour", "zones", "zone_neighbours", "unresolved_event_times", "training_zone_pair_reference"]:
        if name in table_records:
            lines.append(f"| {name} | {table_records[name]['rows']:,} |")
    lines += ["", "Processed source months: " + ", ".join(report["processed_source_months"]) + ".",
              "Boundary source files do not define analytical periods; their out-of-domain events are counted separately.",
              "", "| Saved hourly coverage measure | Zone-hours |", "|---|---:|"]
    for name, count in report["coverage"].items():
        lines.append(f"| {name} | {count:,} |")
    lines += ["", "Resolved UTC hourly keys are unique. Fixed UTC−05:00 is used for weather alignment, while calendar/split labels use America/New_York civil dates.",
              "Ambiguous/nonexistent timestamps remain in explicit unresolved buckets; missing months are not zeros.",
              "Training-only exact OD medians use pooled eligible trips and exclude outcomes beyond the training cutoff; sparse support is descriptive at 30 trips.",
              "Fare means use recorded valid-value counts. Per-trip fare/minute is averaged per trip rather than computed as a ratio of pooled sums.",
              "OPM holidays are verified against the saved official-page transcription. NOAA observations are usable only after CSV clocks are verified against raw UTC reports; product, units and quality qualifiers are retained.",
              "Contemporaneous realised weather is descriptive and its publication latency is unverified.",
              "", "Limitations: provider-specific chronology exclusions can change the demand proxy; original report collision/full-year claims remain unverified; adjacent-file guards do not prove absence of longer-wait censoring.",
              "", "Remaining blockers:"]
    lines += [f"- {item}" for item in report.get("blockers", [])] or ["- None recorded by this run."]
    lines += ["", "Full schemas, units, feature roles and null policies are in data_dictionary.json; exact measured audits are in run_summary.json."]
    (folder / "data_preparation_report_draft.md").write_text("\n".join(lines) + "\n")
    print("Handover:", folder, flush=True)
    return str(folder)


def run_integration(root, inventory, *, sample=False):
    """Execute sources, monthly cleaning, integration and exact training-only OD estimates."""
    from pyspark import StorageLevel
    from pyspark.sql import functions as F
    from src.phase1_sources import SOURCE_MONTHS
    from src.phase1_tables import (
        add_trip_time_fields, build_training_pair_reference, build_zone_hour_table,
        build_zone_pair_table, check_unique_keys, combine_activity_partials,
        combine_provider_audits, integrate_weather_calendar,
    )
    versions = runtime_versions()
    run_id = f"{'sample' if sample else 'available'}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    output = root / "data/processed/phase1" / run_id
    output.mkdir(parents=True, exist_ok=False)
    spark = start_spark()
    try:
        zones, weather, calendar, tables, blockers = load_reference_sources(root, inventory, spark, output)
        note_progress(root, "reference_sources", "complete", output=str(output), blockers=blockers)
        months = clean_available_months(root, inventory, spark, versions, sample=sample)
        for month in months:
            saved = spark.read.parquet(month["output_path"])
            source_columns = json.loads(Path(month["audit_path"]).read_text())["raw_columns"]
            tables[f"cleaned_trips_{month['source_month']}"] = {"path": month["output_path"],
                "rows": month["rows"], "schema": saved.schema.jsonValue(), "source_columns": source_columns,
                "role": "validated cleaned source; integration sample may use only 10000 rows"}
        partials = [integrate_month(root, month, spark, sample=sample) for month in months]
        collisions, collision_proof = cross_file_collision_audit(spark, months, partials)
        tables["cross_file_collision_groups"] = save_table(collisions, output / "cross_file_collision_groups")
        observed = combine_activity_partials(union_saved_tables(spark, partials, "activity"))
        unresolved = union_saved_tables(spark, partials, "unresolved").groupBy(
            "zone_id", "event_type", "time_status", "event_date_nyc", "original_civil_time"
        ).agg(F.sum("event_count").alias("event_count"))
        tables["unresolved_event_times"] = save_table(unresolved, output / "unresolved_event_times")
        provider = combine_provider_audits(union_saved_tables(spark, partials, "provider_exclusions"))
        tables["provider_exclusions"] = save_table(provider, output / "provider_exclusions")
        available = [month["source_month"] for month in months]
        hourly = build_zone_hour_table(spark, zones, observed, unresolved, available,
            start_civil="2024-01-01", end_civil="2024-02-02" if sample else "2025-02-01", is_sample=sample)
        hourly = integrate_weather_calendar(hourly, weather, calendar)
        tables["zone_hour"] = save_table(hourly, output / "zone_hour", ["cohort", "event_month"])
        check_unique_keys(spark.read.parquet(str(output / "zone_hour")), ["zone_id", "hour_start_utc"])
        note_progress(root, "zone_hour", "complete", path=tables["zone_hour"]["path"],
                      rows=tables["zone_hour"]["rows"])

        # Reuse saved cleaned trips. Only the narrow OD fields are cached on disk, never a year in Python RAM.
        pair_facts = None
        for month in months:
            trips = spark.read.parquet(month["output_path"])
            if sample:
                trips = trips.limit(10_000)
            timed = add_trip_time_fields(trips.select(
                "request_datetime", "pickup_datetime", "dropoff_datetime", "pickup_zone_id", "dropoff_zone_id",
                "trip_miles", "trip_seconds", "is_simulation_eligible"))
            timed = timed.select("request_datetime", "dropoff_datetime", "pickup_zone_id", "dropoff_zone_id",
                                 "trip_miles", "trip_seconds", "is_simulation_eligible", "request_time_status", "request_period")
            pair_facts = timed if pair_facts is None else pair_facts.unionByName(timed)
        pair_facts = pair_facts.persist(StorageLevel.DISK_ONLY)
        try:
            reference = build_training_pair_reference(pair_facts, zones)
            tables["training_zone_pair_reference"] = save_table(reference, output / "training_zone_pair_reference")
            labels = ["study_2024", "validation", "test", "drift"]
            if not sample:
                labels += [f"2024-{month:02d}" for month in range(1, 13)] + ["2025-01"]
            for label in labels:
                pairs = build_zone_pair_table(pair_facts, zones, label)
                tables[f"zone_pairs_{label}"] = save_table(pairs, output / "zone_pairs" / label)
                note_progress(root, "zone_pairs", "complete", period=label,
                              path=tables[f"zone_pairs_{label}"]["path"],
                              rows=tables[f"zone_pairs_{label}"]["rows"])
        finally:
            pair_facts.unpersist()
        checked_source_months = {row["source_month"] for row in inventory if row["kind"] == "trips" and row["status"] == "checked"}
        missing = [month for month in SOURCE_MONTHS if month not in checked_source_months]
        blockers += [f"Missing checked trip source month: {month}" for month in missing]
        target_table = spark.read.parquet(str(output / "zone_hour"))
        coverage_expressions = [F.count(F.lit(1)).alias("zone_hours"),
            F.count(F.when(F.col("request_coverage_complete"), 1)).alias("request_coverage_zone_hours"),
            F.count(F.when(F.col("has_primary_weather"), 1)).alias("primary_weather_zone_hours"),
            F.count(F.when(F.col("has_verified_holiday_calendar"), 1)).alias("verified_holiday_zone_hours")]
        for name in ["temperature_c", "precipitation_previous_hour_mm", "wind_metres_per_second", "visibility_metres"]:
            coverage_expressions.append(F.count(F.when(F.col(name).isNotNull(), 1)).alias(f"valid_{name}_zone_hours"))
        coverage = target_table.agg(*coverage_expressions).first().asDict()
        # Verify pooled resolved-event totals after final joins and writes.
        pooled_totals = {}
        for event in ["request", "pickup", "dropoff"]:
            observed_total = target_table.agg(F.sum(f"{event}_count_observed")).first()[0] or 0
            expected_total = sum(record["event_reconciliation"][event]["resolved_in_scope"] for record in partials)
            if observed_total != expected_total:
                raise ValueError(f"Final {event} total changed: {observed_total} != {expected_total}")
            pooled_totals[event] = observed_total
        cleaning_counts = {}
        for month in months:
            if "audit_path" not in month:
                continue
            cleaning_audit = json.loads(Path(month["audit_path"]).read_text())
            role = month.get("role", "analysis")
            combined = cleaning_counts.setdefault(role, {})
            for name, value in cleaning_audit["counts"].items():
                combined[name] = combined.get(name, 0) + value
        report = {"status": "sample_only" if sample else "incomplete" if blockers else "verified_phase1",
                  "is_sample": sample, "versions": versions, "processed_source_months": available,
                  "blockers": blockers, "coverage": coverage, "monthly_event_audits": [record["event_reconciliation"] for record in partials],
                  "pooled_resolved_event_totals": pooled_totals, "validated_cleaning_source_counts": cleaning_counts,
                  "source_month_provenance": months, "tables": tables,
                  "source_inventory": inventory,
                  "not_processed_in_this_scope": [month for month in checked_source_months if month not in available],
                  "cross_file_collision_audit": collision_proof,
                  "unresolved_report_collision_claim": True,
                  "boundary_limitation": "Adjacent-file guards reduce censoring; unbounded request-to-pickup delay means complete real-world demand cannot be guaranteed"}
        report["handover_path"] = export_handover(root, spark, output, tables, report, sample=sample)
        report["audit_path"] = str(root / "data/audits/phase1" / f"{run_id}.json")
        write_json_new(report["audit_path"], report)
        note_progress(root, "integration", report["status"], audit=report["audit_path"],
                      processed_source_months=available)
        print("Phase 1 status:", report["status"], "coverage:", coverage, flush=True)
        return report
    finally:
        spark.stop()


def execute_notebook(root, path, mode):
    """Start a fresh kernel, retain a complete/failure snapshot, and never overwrite data outputs."""
    import nbformat
    from nbclient import NotebookClient
    kernel_root = root / ".venv/share/jupyter"
    kernel_folder = kernel_root / "kernels/ads-etl"
    kernel_folder.mkdir(parents=True, exist_ok=True)
    specification = {"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                     "display_name": "ADS ETL (.venv)", "language": "python"}
    kernel_path = kernel_folder / "kernel.json"
    if kernel_path.exists() and json.loads(kernel_path.read_text()) != specification:
        raise ValueError("Existing kernel specification differs; preserved for review")
    if not kernel_path.exists():
        write_json_new(kernel_path, specification)
    os.environ["JUPYTER_PATH"] = str(kernel_root) + os.pathsep + os.environ.get("JUPYTER_PATH", "")
    os.environ["ADS_PHASE1_MODE"] = mode
    os.environ["ADS_RUN_FULL_MONTH"] = "0"  # January cleaning is reused, not rerun.
    notebook = nbformat.read(path, as_version=4)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            cell.outputs, cell.execution_count = [], None
    snapshot = root / "data/audits/phase1" / f"notebook-{path.stem}-{mode}-{uuid.uuid4().hex[:10]}.ipynb"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    print("Starting fresh notebook:", path.name, mode, flush=True)
    try:
        NotebookClient(notebook, timeout=14400, kernel_name="ads-etl",
                       resources={"metadata": {"path": str(root)}}).execute()
    except Exception:
        nbformat.write(notebook, snapshot)
        print("Notebook stopped; failure snapshot:", snapshot, flush=True)
        raise
    nbformat.write(notebook, snapshot)
    nbformat.write(notebook, path)
    for cell in notebook.cells:
        for item in cell.get("outputs", []):
            if item.output_type == "stream":
                print(item.text, end="", flush=True)
    print("Notebook complete:", path.name, mode, flush=True)


def validate_reference_refresh_manifest(prior, inventory):
    """Reject stale, sample or incomplete trip/OD checkpoints before reference-only refresh."""
    from src.phase1_sources import SOURCE_MONTHS

    if prior.get("is_sample"):
        raise ValueError("A sample cannot be finalized as full-period output")
    if set(prior.get("processed_source_months", [])) != set(SOURCE_MONTHS):
        raise ValueError("Trip coverage is not complete; resume the available-month runner instead")
    provenance = {row["source_month"]: row for row in prior.get("source_month_provenance", [])}
    if set(provenance) != set(SOURCE_MONTHS):
        raise ValueError("Trip source provenance does not cover every required source month")
    checked = {row.get("source_month"): row for row in inventory
               if row["kind"] == "trips" and row["status"] == "checked"}
    for month in SOURCE_MONTHS:
        if checked.get(month, {}).get("sha256") != provenance[month].get("source_sha256"):
            raise ValueError(f"A trip source changed or is unchecked: {month}")
    if not prior.get("tables", {}).get("zone_hour"):
        raise ValueError("No integrated zone-hour output is saved")
    return True


def finalize_reference_integration(root, inventory, prior_path):
    """Refresh independently acquired references using verified trip/OD outputs, without rerunning cleaning or medians."""
    from pyspark.sql import functions as F
    from src.phase1_tables import calendar_schema, check_unique_keys, integrate_weather_calendar, weather_schema
    prior_path = Path(prior_path)
    prior = json.loads(prior_path.read_text())
    validate_reference_refresh_manifest(prior, inventory)
    for table in prior["tables"].values():
        path = Path(table["path"])
        if not path.is_dir() or parquet_rows(path) != table["rows"]:
            raise ValueError(f"Saved trip/OD checkpoint failed its footer check: {path}")
    run_id = "finalized-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    output = root / "data/processed/phase1" / run_id
    output.mkdir(parents=True, exist_ok=False)
    spark = start_spark()
    try:
        _, weather, calendar, references, blockers = load_reference_sources(root, inventory, spark, output)
        previous = spark.read.parquet(prior["tables"]["zone_hour"]["path"])
        old_rows = previous.count()
        columns_to_drop = ["has_primary_weather", "has_verified_holiday_calendar"]
        for suffix in ["", "_laguardia", "_jfk"]:
            fields = spark.createDataFrame([], schema=weather_schema()).schema.fields
            columns_to_drop += [field.name + suffix for field in fields if field.name != "hour_start_utc"]
        columns_to_drop += [field.name for field in spark.createDataFrame([], schema=calendar_schema()).schema.fields
                            if field.name != "event_date_nyc"]
        base = previous.drop(*columns_to_drop)
        refreshed = integrate_weather_calendar(base, weather, calendar)
        check_unique_keys(refreshed, ["zone_id", "hour_start_utc"])
        tables = dict(prior["tables"])
        tables.update(references)
        tables["zone_hour"] = save_table(refreshed, output / "zone_hour", ["cohort", "event_month"])
        if tables["zone_hour"]["rows"] != old_rows:
            raise ValueError("Reference refresh changed the fact-row count")
        refreshed = spark.read.parquet(tables["zone_hour"]["path"])
        for event in ["request", "pickup", "dropoff"]:
            total = refreshed.agg(F.sum(f"{event}_count_observed")).first()[0] or 0
            if total != prior["pooled_resolved_event_totals"][event]:
                raise ValueError("Reference-only refresh changed an observed event total")
        # A metadata-only rename avoids recomputing any already verified OD medians.
        for name, table_record in list(tables.items()):
            if name.startswith("zone_pairs_") or name == "training_zone_pair_reference":
                table = spark.read.parquet(table_record["path"])
                if "reference_available_from_nyc" in table.columns:
                    table = table.withColumnRenamed("reference_available_from_nyc", "reference_period_end_nyc")
                    tables[name] = save_table(table, output / "pair_metadata" / name)
        expressions = [F.count(F.lit(1)).alias("zone_hours"),
            F.count(F.when(F.col("request_coverage_complete"), 1)).alias("request_coverage_zone_hours"),
            F.count(F.when(F.col("has_primary_weather"), 1)).alias("primary_weather_zone_hours"),
            F.count(F.when(F.col("has_verified_holiday_calendar"), 1)).alias("verified_holiday_zone_hours")]
        for name in ["temperature_c", "precipitation_previous_hour_mm", "wind_metres_per_second", "visibility_metres"]:
            expressions.append(F.count(F.when(F.col(name).isNotNull(), 1)).alias(f"valid_{name}_zone_hours"))
        coverage = refreshed.agg(*expressions).first().asDict()
        source_ranges = []
        for record in inventory:
            if record["kind"] != "trips" or record["status"] != "checked":
                continue
            source = spark.read.parquet(record["path"])
            range_expressions = [F.count(F.lit(1)).alias("source_rows")]
            for event in ["request_datetime", "pickup_datetime", "dropoff_datetime"]:
                range_expressions += [F.min(event).alias("min_" + event), F.max(event).alias("max_" + event)]
            if "cbd_congestion_fee" in source.columns:
                range_expressions += [F.count(F.when(F.col("cbd_congestion_fee").isNull(), 1)).alias("missing_cbd_congestion_fee"),
                                      F.count(F.when(F.col("cbd_congestion_fee") < 0, 1)).alias("negative_cbd_congestion_fee")]
            measured = source.agg(*range_expressions).first().asDict()
            if measured["source_rows"] != record["rows"]:
                raise ValueError("Source range audit disagrees with its Parquet footer")
            source_ranges.append({"source_month": record["source_month"], "source_sha256": record["sha256"], **measured})
        report = {**prior, "status": "incomplete" if blockers else "verified_phase1", "blockers": blockers,
                  "versions": runtime_versions(), "coverage": coverage, "tables": tables,
                  "source_inventory": inventory, "observed_raw_source_ranges": source_ranges,
                  "reference_refresh_from": str(prior_path),
                  "reused_work": "All validated trip cleaning, month activity partials and exact OD estimates retained unchanged"}
        report["handover_path"] = export_handover(root, spark, output, tables, report, sample=False)
        report["audit_path"] = str(root / "data/audits/phase1" / f"{run_id}.json")
        write_json_new(report["audit_path"], report)
        note_progress(root, "reference_finalization", report["status"], audit=report["audit_path"],
                      reused_from=str(prior_path))
        print("Reference finalization:", report["status"], coverage, flush=True)
        return report
    finally:
        spark.stop()


def run_preflight(root):
    """Run tests and Ruff before the bounded real-data run; retain exact command results."""
    result = {"versions": runtime_versions(), "checks": []}
    for arguments in [["-m", "pytest", "tests", "-q", "--tb=short"], ["-m", "ruff", "check", "."]]:
        check = subprocess.run([sys.executable, *arguments], cwd=root, capture_output=True, text=True)
        result["checks"].append({"command": arguments, "returncode": check.returncode,
                                 "stdout": check.stdout, "stderr": check.stderr})
        print(check.stdout, flush=True)
        if check.returncode:
            receipt = root / "data/audits/phase1" / f"validation-failed-{uuid.uuid4().hex[:10]}.json"
            write_json_new(receipt, result)
            raise RuntimeError(f"Preflight failed; see {receipt}")
    receipt = root / "data/audits/phase1" / f"validation-passed-{uuid.uuid4().hex[:10]}.json"
    write_json_new(receipt, result)
    os.environ["ADS_PREFLIGHT_COMPLETE"] = "1"
    return receipt


def main():
    """Acquire official files or inspect local sources without claiming missing coverage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquire-only", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--acquire", action="store_true", help="Acquire missing sources after the early developer sample")
    parser.add_argument("--sample-only", action="store_true")
    parser.add_argument("--finalize-latest", action="store_true", help="Refresh references after completed trip/OD processing; no repeat cleaning or medians")
    parser.add_argument("--resume-available", action="store_true", help="Resume full integration from checked inputs and compatible saved cleaning/month checkpoints")
    arguments = parser.parse_args()
    runtime_versions()
    acquire_sources(ROOT, acquire=arguments.acquire_only)
    if arguments.acquire_only or arguments.inventory_only:
        return
    print("Validation receipt:", run_preflight(ROOT), flush=True)
    execute_notebook(ROOT, ROOT / "notebooks/01_sources.ipynb", "sample")
    if arguments.finalize_latest:
        candidates = sorted((ROOT / "data/audits/phase1").glob("available-*.json"))
        if not candidates:
            raise RuntimeError("No completed available-input run to finalize yet")
        os.environ["ADS_PHASE1_FINALIZE_MANIFEST"] = str(candidates[-1])
        execute_notebook(ROOT, ROOT / "notebooks/01_sources.ipynb", "available")
        execute_notebook(ROOT, ROOT / "notebooks/01_integrate.ipynb", "finalize")
        return
    if arguments.resume_available:
        note_progress(ROOT, "resume", "started", inventory_path=os.environ.get("ADS_PHASE1_INVENTORY_PATH"))
        execute_notebook(ROOT, ROOT / "notebooks/01_sources.ipynb", "available")
        previous = {str(path) for path in (ROOT / "data/audits/phase1").glob("available-*.json")}
        execute_notebook(ROOT, ROOT / "notebooks/01_integrate.ipynb", "resume")
        candidates = sorted(path for path in (ROOT / "data/audits/phase1").glob("available-*.json")
                            if str(path) not in previous)
        if not candidates:
            raise RuntimeError("Integration did not save an available-period manifest")
        os.environ["ADS_PHASE1_FINALIZE_MANIFEST"] = str(candidates[-1])
        execute_notebook(ROOT, ROOT / "notebooks/01_integrate.ipynb", "finalize")
        return
    execute_notebook(ROOT, ROOT / "notebooks/01_etl.ipynb", "sample")
    execute_notebook(ROOT, ROOT / "notebooks/01_integrate.ipynb", "sample")
    if arguments.sample_only:
        print("Bounded sample batch complete. Required full-period coverage is not claimed.", flush=True)
        return
    if arguments.acquire:
        acquire_sources(ROOT, acquire=True)
        execute_notebook(ROOT, ROOT / "notebooks/01_sources.ipynb", "available")
    execute_notebook(ROOT, ROOT / "notebooks/01_integrate.ipynb", "available")
    print("Available-input batch finished. Read the status/coverage manifest before claiming Phase 1 complete.", flush=True)


if __name__ == "__main__":
    main()
