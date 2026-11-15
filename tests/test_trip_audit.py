"""Check the extracted audit contracts on existing synthetic cleaner fixtures."""

from test_trip_cleaning import KNOWN_ZONE_IDS, RAW_SCHEMA, make_trip

from src.trip_audit import count_filter_stages, count_key_groups, count_trip_flags
from src.trip_cleaning import QUALITY_FAILURE_FLAGS, clean_trips


def test_audit_reconciles_collisions_and_overlapping_failure_flags(phase1_spark):
    raw = phase1_spark.createDataFrame([make_trip(), make_trip(), make_trip(trip_time=0)], RAW_SCHEMA)
    flagged = clean_trips(raw, KNOWN_ZONE_IDS)
    counts = count_trip_flags(flagged, ["trip_seconds"])
    assert counts["input_rows"] == 3 and counts["is_accepted"] == 2
    assert counts["has_repeated_key"] == 3  # Duration is intentionally not in the agreed key.
    assert count_filter_stages(flagged)[QUALITY_FAILURE_FLAGS[-1]] == 2
    assert count_key_groups(flagged) == [{"records_per_key": 3, "records": 3, "key_groups": 1}]
