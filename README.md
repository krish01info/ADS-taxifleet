# Where Should the Robotaxis Wait? Profit-Driven Fleet Placement

Advanced Data Science (ADS) end-term project, 2026.

A robotaxi company has no drivers, so it decides where every car waits, and every idle hour and empty mile is a direct cost. This project uses 2024 NYC Uber/Lyft trips as a stand-in for robotaxi demand to answer three questions for a possible New York City launch:

- Which zones should form the launch area?
- How many cars should wait in each zone each hour?
- When a car is freed in a quiet zone, should it stay or drive empty to a busier one?

```text
Raw trips + zones + weather + holidays
   → Spark ETL → zone-hour and zone-pair tables (SSOT)
   → EDA and H1 → demand forecast → fleet simulator + launch-area optimisation
   → H2–H5 tests → placement table, wait-or-move rule, deployment, drift check
```

## Team

| Who | Role |
|---|---|
| Krish Sharma (240514) | Maintainer: reviews and merges every pull request into `main`. |
| Harsh Rohila (240466), Himank Swami (240474), Harsh Vardhan (240467) | Team members. |

## Status

Phase 1 is verified on `himank/phase1-etl`, completed by Himank; data publication is complete and merge into main remains pending. Next work is EDA/H1, forecasting and simulation preparation; assignments have not been set here. Read [Phase 1 handover](docs/phase1_handover.md) and [progress](PROGRESS.md).

## Start here for teammates and AI assistants

The [shared handover](docs/phase1_handover.md) lists exact tables, schemas, units, coverage and limitations. Obtain the published versioned data bundle there. Do not restart ETL to begin downstream development.

```text
Continue ADS-taxifleet in my local clone on my assigned task branch. Read AGENTS.md, repo_instructions.md, README.md, PROGRESS.md and docs/phase1_handover.md first. Inspect my branch, commit, local paths and data version; obtain, extract and verify phase1-2024-v1 if absent. Explain the pipeline and relevant tables simply, complete required understanding checks, then continue my assigned task using the finalized data without restarting ETL. Never push, only I push.
```

## Environment and quick start

Use Python 3.11 and Java 17 or 21. All dependencies are pinned in `requirements.txt` (Spark 4.0.1, PyArrow 24.0.0, PyShp 2.3.1, Shapely 2.1.2, pyproj 3.7.2 and notebook/test tools).

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Download/extract/verify using the handover commands. Published release: [phase1-2024-v1](https://github.com/krish01info/ADS-taxifleet/releases/tag/phase1-2024-v1). [Archive](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/ads-taxifleet-phase1-2024-v1.tar.gz) · [SHA256SUMS](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/SHA256SUMS).

Himank confirmed that GitHub asset metadata shows both assets uploaded and the archive's 355,388,882-byte size and SHA-256 match the previously verified local archive. The release tag points to `64d8663ba5a2020b62e97a6fc558c02be00ac571`. This records metadata verification, not a fresh download-and-hash check of the hosted archive.

Authoritative portable paths: `data/releases/phase1-2024-v1/metadata/table_index.json`, `run_summary.json`, `data_dictionary.json`, `file_manifest.json`, and `validation-passed-1d8df0edf9.json` in that same metadata directory. Exact physical table paths and individual sizes are in the [handover table](docs/phase1_handover.md#pipeline-and-authoritative-artifacts); some outputs intentionally remain under the earlier available-run directory.

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


Parquet outputs can be directories; use Hive partition discovery. Train Jan–Aug 2024, validate Sep–Oct, test Nov–Dec; Jan 2025 is drift-only. Dec 2023/Feb 2025 are boundary guards. Null coverage is not zero demand; preserve monetary nulls and valid-value counts. Use UTC keys and civil New York dates; do not guess DST folds. Sparse OD medians remain observed, unsupported values null; training references and predictor timing must prevent future leakage. See the shared handover for details.

## Reproduce or resume (separate from data use)

```bash
# New environment/raw acquisition and bounded gates, then full processing:
.venv/bin/python notebooks/run_phase1.py --acquire
# Resume checked inputs and compatible validated checkpoints:
.venv/bin/python notebooks/run_phase1.py --resume-available
# Refresh only references after a complete available manifest:
.venv/bin/python notebooks/run_phase1.py --finalize-latest
```

Run in a normal terminal with local sockets enabled. These commands are expensive; published finalized data needs none of them. Notebooks `01_sources`, `01_etl`, `01_integrate` explain the stages. Saved checkpoints are reused only with matching source/code provenance and footer counts. Raw acquisition is required for a complete rebuild; the compact bundle intentionally excludes raw and full cleaned trips.

## Folders

| Folder | Contents |
|---|---|
| `docs/` | The plan (pre-submission summary) and the list of decisions. |
| `data/` | Raw and processed data. Not committed. |
| `notebooks/` | The work, in numbered Jupyter notebooks (from Phase 1). |
| `src/` | The few key functions the notebooks import: trip cleaning, cost and profit, empty-trip miles (from Phase 1). |
| `tests/` | pytest tests for `src/`, on a small sample (from Phase 1). |

## Contributing

Read [`repo_instructions.md`](repo_instructions.md) first.
