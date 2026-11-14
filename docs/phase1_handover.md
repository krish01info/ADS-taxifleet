# Phase 1 teammate handover

Phase 1 preparation and integration is verified on `himank/phase1-etl`, completed by Himank. Data publication is complete; merge into main remains pending. No downstream phase is complete. Version: `finalized-20261114T190620-6b22f8cb`; published release tag: `phase1-2024-v1`, pointing to `64d8663ba5a2020b62e97a6fc558c02be00ac571`.

## Pipeline and authoritative artifacts

Official trip files → unchanged validated cleaner → separate event time resolution → per-source additive activity → zone-hour grid → weather/calendar joins. Separately, eligible trips feed pooled exact OD medians. Raw files and validated outputs are preserved; teammates do not need to reclean or recompute medians.

Portable index: `data/releases/phase1-2024-v1/metadata/table_index.json`. Portable measured manifest: `data/releases/phase1-2024-v1/metadata/run_summary.json`. Dictionary with every column's type, units, definition and role: `data/releases/phase1-2024-v1/metadata/data_dictionary.json`. The local original manifest is `data/audits/phase1/finalized-20261114T190620-6b22f8cb.json`; its snapshot may contain original-machine absolute paths, so use the portable index for loading. Raw/full cleaned provenance paths in original audits are intentionally excluded from the bundle.

| Table | Rows | Parquet bytes | Authoritative directory |
|---|---:|---:|---|
| `zones` | 263 | 1,050,282 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/zones` |
| `zone_neighbours` | 692 | 9,506 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/zone_neighbours` |
| `weather_observations` | 28,514 | 1,444,655 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/weather_observations` |
| `weather_station_hour` | 28,509 | 2,184,606 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/weather_station_hour` |
| `weather_station_checks` | 2 | 4,653 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/weather_station_checks` |
| `calendar` | 397 | 8,545 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/calendar` |
| `cross_file_collision_groups` | 0 | 1,247 | `data/processed/phase1/available-20261114T180856-e39a9abe/cross_file_collision_groups` |
| `unresolved_event_times` | 161,300 | 980,927 | `data/processed/phase1/available-20261114T180856-e39a9abe/unresolved_event_times` |
| `provider_exclusions` | 9,389,821 | 71,096,830 | `data/processed/phase1/available-20261114T180856-e39a9abe/provider_exclusions` |
| `zone_hour` | 2,505,864 | 281,822,289 | `data/processed/phase1/finalized-20261114T190620-6b22f8cb/zone_hour` |
| `training_zone_pair_reference` | 69,169 | 1,578,117 | `data/processed/phase1/available-20261114T180856-e39a9abe/training_zone_pair_reference` |
| `zone_pairs_study_2024` | 69,169 | 1,588,420 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/study_2024` |
| `zone_pairs_validation` | 69,169 | 1,526,250 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/validation` |
| `zone_pairs_test` | 69,169 | 1,518,568 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/test` |
| `zone_pairs_drift` | 69,169 | 1,484,655 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/drift` |
| `zone_pairs_2024-01` | 69,169 | 1,484,209 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-01` |
| `zone_pairs_2024-02` | 69,169 | 1,486,365 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-02` |
| `zone_pairs_2024-03` | 69,169 | 1,492,873 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-03` |
| `zone_pairs_2024-04` | 69,169 | 1,492,538 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-04` |
| `zone_pairs_2024-05` | 69,169 | 1,496,820 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-05` |
| `zone_pairs_2024-06` | 69,169 | 1,498,057 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-06` |
| `zone_pairs_2024-07` | 69,169 | 1,494,240 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-07` |
| `zone_pairs_2024-08` | 69,169 | 1,495,856 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-08` |
| `zone_pairs_2024-09` | 69,169 | 1,494,209 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-09` |
| `zone_pairs_2024-10` | 69,169 | 1,494,165 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-10` |
| `zone_pairs_2024-11` | 69,169 | 1,489,688 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-11` |
| `zone_pairs_2024-12` | 69,169 | 1,489,605 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2024-12` |
| `zone_pairs_2025-01` | 69,169 | 1,485,375 | `data/processed/phase1/available-20261114T180856-e39a9abe/zone_pairs/2025-01` |

Parquet tables may be directories containing many part files and Hive partitions. Read the directory with partition discovery; do not load only one part file. Schemas were checked against actual Parquet datasets, and all saved footer counts match the manifest.

Zone-hour key: `(zone_id, hour_start_utc)`. OD key within a table: `(pickup_zone_id, dropoff_zone_id, period_label)`. Zone key: `zone_id`. Neighbour key: `(zone_id, neighbour_zone_id)`; 692 directed edges include shared-boundary and point-touch indicators. Weather key: `(station_id, hour_start_utc)`. Calendar key: `event_date_nyc`.

Zone attributes include borough, name, original CRS, centroid, area in square metres and geometry WKB. Distances use miles except explicitly named metres; durations use minutes in summaries and seconds in cleaned trips; monetary values are USD. Weather uses °C, mm previous-hour precipitation, m/s wind, metres visibility and percent humidity. `request_count` is eligible completed-trip requests, not total unfulfilled marketplace demand.

## Choose inputs for your task

- EDA/H1: zone-hour demand/activity/value summaries plus valid monetary denominators and provider diagnostics. Do not infer trip-level distributions from hourly means. Trip-level H1 analyses requiring individual observations need the separately reproducible cleaned-trip dataset; full cleaned trips are intentionally absent from this compact release.
- Forecasting: zone-hour table, calendar, zones and coverage masks; target `request_count`. Respect chronological boundaries and predictor availability.
- Simulation/launch-area: training-only OD reference, zones and neighbours, plus held-out demand/activity. Observed trips cannot alone reconstruct unfulfilled demand or a complete simulator. Fleet/cost/profit implementation remains downstream.

## Coverage and limitations

263 zones × 9,528 real hours = 2,505,864 rows, including 2024 leap day and civil DST transitions. All 13 analysis months and two guard sources were checked. All holiday zone-hours have verified calendar data. Combined cleaned scope: 259,885,329 rows = 257,269,699 accepted + 2,615,630 quarantined, including only relevant slices of guards.

| Measure | Present zone-hours | Missing/incomplete |
|---|---:|---:|
| `request_coverage_zone_hours` | 2,505,338 | 526 |
| `primary_weather_zone_hours` | 2,501,393 | 4,471 |
| `verified_holiday_zone_hours` | 2,505,864 | 0 |
| `valid_temperature_c_zone_hours` | 2,493,766 | 12,098 |
| `valid_precipitation_previous_hour_mm_zone_hours` | 2,351,220 | 154,644 |
| `valid_wind_metres_per_second_zone_hours` | 2,250,491 | 255,373 |
| `valid_visibility_metres_zone_hours` | 2,498,237 | 7,627 |

Train is January–August 2024, validation September–October, test November–December; January 2025 is reserved for drift. Periods follow New York civil event dates, not source filenames or fixed EST dates. December 2023 and February 2025 are adjacent source guards only; their relevant events repair file-boundary coverage, never extend the analysis period.

The UTC hour key resolves real instants. `hour_start_est` uses fixed UTC−05:00; `event_date_nyc`, `hour_nyc`, offset and DST fields use America/New_York. Ambiguous/nonexistent offset-free source times stay in `unresolved_event_times`; no folds were guessed. There are 59,615 request, 61,531 pickup and 54,468 drop-off unresolved eligible events. These event totals differ from the 161,300 grouped unresolved rows.

Accepted/quarantined partition all input records; eligibility flags overlap. Mapped pickups can contribute demand even with unmapped destinations (recognised 264/265 differ from unknown IDs). Simulation needs usable mapped endpoints. Duration/speed use `trip_time`, not timestamp differences. Every collision-key record is retained; matching the six fields does not prove duplicate identity. Monetary null/negative values remain flagged; never replace them with zero. Monetary means carry valid-value denominators (`valid_fare_count`, `valid_tolls_count`, `monetary_trip_count`); base fare is revenue and tips are excluded.

`request_count` is null when hourly coverage is unresolved; `request_count_observed` retains the resolved partial count. Covered empty hours have genuine zero demand. Never treat a missing source period or unresolved hour as zero. Adjacent files reduce completed-trip censoring but cannot prove complete real-world requests. Provider chronology exclusion differs (January Uber 0.6482%, Lyft 1.7883%); the provider/zone/hour diagnostic table preserves both recorded request and pickup views. Do not guess repaired timestamps.

OD support labels are 0 unsupported, 1–29 sparse, 30+ supported. Keep sparse observed medians; unsupported medians are null. Thirty is descriptive support, not statistical confidence. Use only `training_zone_pair_reference` for downstream evaluation references: it pools eligible training observations and excludes drop-offs crossing September 1. Descriptive test/year OD tables must not inform training, tuning, thresholds or evaluated travel references. Straight-line centroid miles are a transparent fallback input, not observed trip miles or travel minutes.

Weather is retrospective observed LCDv1 data with original values, quality labels and explicit SI conversion. Central Park is primary; LaGuardia/JFK are checks, never silent replacements. Observation time is not verified publication time. Current-hour observed weather, realised fares/durations and future drop-offs cannot silently become forecasting predictors. Use historical lags or an independently justified availability policy. Trace precipitation is null with a trace flag, not zero. One conflicting LaGuardia routine timestamp is audited and excluded. Federal holiday dates come from a provenance-labelled OPM transcription. The original report's undocumented collision rate remains unverified.


## Loading without rebuilding

```python
import json
import pyarrow.dataset as ds
from pathlib import Path
index = json.loads(Path("data/releases/phase1-2024-v1/metadata/table_index.json").read_text())
hours = ds.dataset(index["zone_hour"]["path"], format="parquet", partitioning="hive")
train = hours.scanner(
    columns=["zone_id", "hour_start_utc", "request_count", "request_coverage_complete"],
    filter=ds.field("period_label") == "train", batch_size=10000)
first_batch = train.head(3)
pairs = ds.dataset(index["training_zone_pair_reference"]["path"], format="parquet")
print(first_batch.slice(0, 3), pairs.count_rows())
```


The loading contract was exercised with bounded PyArrow batches; each OD table contains 69,169 rows. For small development samples use an explicit scanner limit; never label a sample as full-period coverage.

## Validation and version verification

`data/releases/phase1-2024-v1/metadata/validation-passed-1d8df0edf9.json`: 57 tests passed and Ruff passed before the successful run. Fresh integration/finalizer kernels reached their final cells; the final manifest records row/event invariance. Packaged `data/releases/phase1-2024-v1/metadata/reference_source_evidence.json` records station clocks/quality. Per-month cleaning audits, source inventory, ranges, event reconciliation and units accompany the manifest. `data/releases/phase1-2024-v1/verification.json` records local archive verification.

All archive members were hashed and compared with originals; a temporary bounded extraction decoded 263 zones. Full-year ETL was not rerun for packaging. No downstream model, simulator or hypothesis result is claimed.

## Obtain, extract and verify

Publication is **complete**. Download from the live [release page](https://github.com/krish01info/ADS-taxifleet/releases/tag/phase1-2024-v1), [data archive](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/ads-taxifleet-phase1-2024-v1.tar.gz), and [checksums](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/SHA256SUMS). Himank confirmed GitHub asset metadata shows both assets uploaded, with archive size and SHA-256 matching the previously verified local archive. The release tag targets `64d8663ba5a2020b62e97a6fc558c02be00ac571`. This is metadata verification; a fresh download-and-hash check of the hosted archive has not been recorded.

Archive: 355,388,882 bytes. SHA-256: `fa96ebbf0a8663167036089582ced3cb4ce6bc311a57877875af374c04d45eea`. Extract from repo root; the archive restores `data/processed/...` and `data/releases/.../metadata` paths without relocating large tables.

```bash
# After placing both release assets in the repository root:
sha256sum -c SHA256SUMS
tar -tzf ads-taxifleet-phase1-2024-v1.tar.gz | head
tar -xzkf ads-taxifleet-phase1-2024-v1.tar.gz
# -k protects existing files; use a fresh clone if it reports conflicts.
.venv/bin/python - <<'PYCODE'
import hashlib, json
from pathlib import Path
for row in json.loads(Path("data/releases/phase1-2024-v1/metadata/file_manifest.json").read_text()):
    path = Path(row["path"])
    assert path.stat().st_size == row["bytes"], path
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == row["sha256"], path
print("All shared files verified")
PYCODE
```

## Published release and pending merge review

The release is already published against the completed Phase 1 commit. Keep the existing release tag fixed. To push subsequent documentation corrections and optionally verify a downloaded copy, run:

```bash
git push -u origin himank/phase1-etl
gh release view phase1-2024-v1 --repo krish01info/ADS-taxifleet --json tagName,targetCommitish,assets,url
mkdir -p /tmp/ads-phase1-release-check
gh release download phase1-2024-v1 --repo krish01info/ADS-taxifleet --dir /tmp/ads-phase1-release-check
(cd /tmp/ads-phase1-release-check && sha256sum -c SHA256SUMS)
git ls-remote origin refs/tags/phase1-2024-v1
```

The reported tag target and GitHub asset metadata match the recorded local archive. Teammates should still verify downloaded assets with `SHA256SUMS`. Open the repository's Compare & pull request page manually: base `main`, compare `himank/phase1-etl`; request Krish's review. Include the verified data version, receipt (57 tests/Ruff), last full-run date 2026-11-15 IST, limitations and completed publication status. Merge into main remains pending. No raw/processed data belong in the PR.

## Start here for the next assistant

```text
Continue ADS-taxifleet in my local clone on my assigned task branch. Read AGENTS.md, repo_instructions.md, README.md, PROGRESS.md and docs/phase1_handover.md first. Inspect my branch, commit, local paths and data version; obtain, extract and verify phase1-2024-v1 if absent. Explain the pipeline and relevant tables simply, complete required understanding checks, then continue my assigned task using the finalized data without restarting ETL. Never push, only I push.
```
