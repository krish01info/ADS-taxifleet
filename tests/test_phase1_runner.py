"""Fast validation tests for safe reference-only resume gates."""

import pytest

from notebooks.run_phase1 import clean_available_months, validate_reference_refresh_manifest
from src.phase1_sources import SOURCE_MONTHS


def valid_manifest():
    return {
        "is_sample": False,
        "processed_source_months": list(SOURCE_MONTHS),
        "source_month_provenance": [
            {"source_month": month, "source_sha256": f"hash-{month}"}
            for month in SOURCE_MONTHS
        ],
        "tables": {"zone_hour": {"path": "/unused", "rows": 1}},
    }


def valid_inventory():
    return [
        {"kind": "trips", "status": "checked", "source_month": month,
         "sha256": f"hash-{month}"}
        for month in SOURCE_MONTHS
    ]


def test_reference_refresh_requires_complete_matching_sources():
    assert validate_reference_refresh_manifest(valid_manifest(), valid_inventory())


@pytest.mark.parametrize("change, message", [
    (lambda manifest: manifest.update(is_sample=True), "sample"),
    (lambda manifest: manifest.update(processed_source_months=SOURCE_MONTHS[:-1]), "coverage"),
    (lambda manifest: manifest["source_month_provenance"].pop(), "provenance"),
    (lambda manifest: manifest["tables"].clear(), "zone-hour"),
])
def test_reference_refresh_rejects_incomplete_checkpoint(change, message):
    manifest = valid_manifest()
    change(manifest)
    with pytest.raises(ValueError, match=message):
        validate_reference_refresh_manifest(manifest, valid_inventory())


def test_reference_refresh_rejects_changed_or_unchecked_source():
    inventory = valid_inventory()
    inventory[-1]["status"] = "blocked"
    with pytest.raises(ValueError, match=SOURCE_MONTHS[-1]):
        validate_reference_refresh_manifest(valid_manifest(), inventory)


def test_cached_cleaning_reuse_uses_audit_path_from_verified_checkpoint(tmp_path, monkeypatch):
    import csv
    import json

    from notebooks import run_phase1

    lookup = tmp_path / "data/raw/zones/taxi_zone_lookup.csv"
    lookup.parent.mkdir(parents=True)
    with lookup.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["LocationID"])
        writer.writerow([1])
    audit_path = tmp_path / "data/audits/trip_cleaning/existing-checkpoint.json"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text("{}")
    checkpoint = {
        "source_month": "2024-01",
        "source_sha256": "input-hash",
        "output_path": str(tmp_path / "data/processed/january/trips"),
        "audit_path": str(audit_path),
        "rows": 12,
        "cleaner_source_sha256": "cleaner-hash",
        "reused": True,
    }

    class FakeReader:
        def parquet(self, _path):
            return object()

    class FakeSpark:
        read = FakeReader()

    monkeypatch.setattr(run_phase1, "verified_cleaning_cache", lambda _root, _record: checkpoint.copy())
    inventory_record = {
        "kind": "trips", "status": "checked", "source_month": "2024-01",
        "path": str(tmp_path / "data/raw/trips/january.parquet"),
        "rows": 12, "sha256": "input-hash", "role": "analysis",
    }
    assert "audit_path" not in inventory_record

    result = clean_available_months(
        tmp_path, [inventory_record], FakeSpark(), versions={"python": "test"})

    assert result[0]["audit_path"] == str(audit_path)
    progress = json.loads((tmp_path / "data/inspection/phase1_progress.jsonl").read_text())
    assert progress["audit"] == str(audit_path)
    assert progress["rows"] == 12
