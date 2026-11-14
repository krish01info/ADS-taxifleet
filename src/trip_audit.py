"""Reusable, tested trip audit definitions extracted from the validated January notebook."""

import json
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from pyspark import StorageLevel
from pyspark.sql import functions as F
from src.trip_cleaning import COLLISION_KEY, COLUMN_NAMES, QUALITY_FAILURE_FLAGS, clean_trips

def count_trip_flags(trips, raw_column_names):
    """Return independent flag counts and null counts, without collecting trip rows."""
    boolean_names = [field.name for field in trips.schema if field.dataType.simpleString() == "boolean"]
    expressions = [F.count(F.lit(1)).alias("input_rows")]
    for name in boolean_names:
        expressions.append(F.count(F.when(F.col(name), 1)).alias(name))
        expressions.append(F.count(F.when(F.col(name).isNull(), 1)).alias(f"null_flag_{name}"))
    for name in raw_column_names:
        expressions.append(F.count(F.when(F.col(name).isNull(), 1)).alias(f"missing_{name}"))
    return trips.agg(*expressions).first().asDict()


def count_filter_stages(trips):
    """Return a sequential filter waterfall; no physical trip rows are removed here."""
    passes_so_far = F.lit(True)
    expressions = []
    for name in QUALITY_FAILURE_FLAGS:
        passes_so_far = passes_so_far & ~F.col(name)
        expressions.append(F.count(F.when(passes_so_far, 1)).alias(name))
    return trips.agg(*expressions).first().asDict()


def describe_timestamp_differences(trips):
    """Return signed difference statistics and disjoint absolute-difference bands."""
    difference = F.col("duration_difference_seconds")
    absolute = F.abs(difference)
    bands = {
        "unavailable": difference.isNull(), "exact_match": difference == 0,
        "above_0_through_1_second": (absolute > 0) & (absolute <= 1),
        "above_1_through_60_seconds": (absolute > 1) & (absolute <= 60),
        "above_60_through_300_seconds": (absolute > 60) & (absolute <= 300),
        "above_300_seconds": absolute > 300,
    }
    expressions = [
        F.min(difference).alias("min_signed_seconds"),
        F.max(difference).alias("max_signed_seconds"),
        F.avg(difference).alias("mean_signed_seconds"),
        F.percentile_approx(difference, [0.5, 0.9, 0.99], 10_000).alias("signed_p50_p90_p99"),
        F.percentile_approx(absolute, [0.5, 0.9, 0.99], 10_000).alias("absolute_p50_p90_p99"),
        F.count(F.when(difference < 0, 1)).alias("negative_differences"),
        F.count(F.when(difference > 0, 1)).alias("positive_differences"),
    ]
    for name, condition in bands.items():
        expressions.append(F.count(F.when(condition, 1)).alias(name))
    summary = trips.agg(*expressions).first().asDict()
    summary["band_total"] = sum(summary[name] for name in bands)
    summary["quantile_method"] = "Spark percentile_approx, accuracy 10000"
    return summary


def count_key_groups(trips):
    """Count groups exactly from each row's group size; preserve incomplete keys separately."""
    sizes = trips.filter(F.col("key_record_count").isNotNull()).groupBy("key_record_count").count()
    distribution = []
    for row in sizes.orderBy("key_record_count").collect():
        assert row["count"] % row.key_record_count == 0
        distribution.append({"records_per_key": row.key_record_count,
                             "records": row["count"],
                             "key_groups": row["count"] // row.key_record_count})
    return distribution


def count_zone_categories(trips):
    """Return pickup and destination categories separately, including unmapped/missing IDs."""
    categories = {}
    for endpoint in ["pickup", "dropoff"]:
        categories[endpoint] = [row.asDict() for row in
                                trips.groupBy(f"{endpoint}_zone_status").count()
                                .orderBy(f"{endpoint}_zone_status").collect()]
    return categories

def run_cleaning_scope(raw_scope, expected_rows, scope_name, *, project_root, known_zone_ids, versions, input_hashes):
    """Clean one scope, audit it, and save a lossless partition in a fresh folder."""
    spark = raw_scope.sparkSession
    run_id = f"{scope_name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    output_path = project_root / "data/processed/trip_cleaning" / run_id / "trips"
    audit_path = project_root / "data/audits/trip_cleaning" / f"{run_id}.json"
    raw_names = [COLUMN_NAMES.get(name, name) for name in raw_scope.columns]
    trips = clean_trips(raw_scope, known_zone_ids).persist(StorageLevel.DISK_ONLY)
    try:
        counts = count_trip_flags(trips, raw_names)
        assert counts["input_rows"] == expected_rows, "Flagging changed the row count"
        assert all(value == 0 for name, value in counts.items() if name.startswith("null_flag_"))
        waterfall = count_filter_stages(trips)
        assert waterfall[QUALITY_FAILURE_FLAGS[-1]] == counts["is_accepted"]
        timestamp_summary = describe_timestamp_differences(trips)
        assert timestamp_summary["band_total"] == expected_rows
        keys = count_key_groups(trips)
        assert sum(item["records"] for item in keys) + counts["has_incomplete_key"] == expected_rows
        assert sum(item["records"] for item in keys if item["records_per_key"] > 1) == counts["has_repeated_key"]
        zone_categories = count_zone_categories(trips)
        for endpoint_counts in zone_categories.values():
            assert sum(item["count"] for item in endpoint_counts) == expected_rows
        combinations = [row.asDict() for row in trips.groupBy(
            "cleaning_status", "is_demand_eligible", "is_simulation_eligible",
            "is_fare_analysis_eligible", "is_monetary_eligible",
        ).count().collect()]
        writer = trips.write.mode("errorifexists")
        if expected_rows:
            writer = writer.partitionBy("cleaning_status")
        writer.parquet(str(output_path))
    finally:
        trips.unpersist()

    saved = spark.read.parquet(str(output_path))
    partition_counts = {row.cleaning_status: row["count"] for row in saved.groupBy("cleaning_status").count().collect()}
    accepted = partition_counts.get("accepted", 0)
    quarantined = partition_counts.get("quarantined", 0)
    assert set(partition_counts) <= {"accepted", "quarantined"}
    assert accepted + quarantined == expected_rows
    assert accepted == counts["is_accepted"]
    verification_flags = ["has_repeated_key", "is_demand_eligible", "is_simulation_eligible",
                          "is_fare_analysis_eligible", "is_monetary_eligible"]
    saved_counts = saved.agg(*[F.count(F.when(F.col(name), 1)).alias(name)
                               for name in verification_flags]).first().asDict()
    for name in verification_flags:
        assert saved_counts[name] == counts[name], f"Read-back count changed: {name}"
    audit = {
        "scope": scope_name, "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "cleaner_source_sha256": hashlib.sha256(Path(__file__).with_name("trip_cleaning.py").read_bytes()).hexdigest(),
        "versions": versions, "input_sha256": input_hashes,
        "expected_rows": expected_rows, "counts": counts,
        "sequential_rows_remaining": waterfall, "zone_categories": zone_categories,
        "collision_key": COLLISION_KEY, "collision_group_size_distribution": keys,
        "timestamp_differences": timestamp_summary,
        "eligibility_combinations": combinations,
        "partition_counts": {"accepted": accepted, "quarantined": quarantined},
        "read_back_flag_counts": saved_counts,
        "output_path": str(output_path.relative_to(project_root)),
        "raw_columns": raw_names, "status": "complete",
        "unverified_report_claims": ["full-year totals", "exact duplicates", "original collision-key rate",
                                      "trip-zone join coverage", "weather join coverage"],
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("x") as output:
        json.dump(audit, output, indent=2)
    print("Saved:", output_path.relative_to(project_root))
    print("Audit:", audit_path.relative_to(project_root))
    print("Partition:", audit["partition_counts"])
    print("Independent eligibility:", {name: counts[name] for name in verification_flags[1:]})
    print("Repeated-key records:", counts["has_repeated_key"])
    print("Filter waterfall:", waterfall)
    print("Timestamp differences:", timestamp_summary)
    return audit
