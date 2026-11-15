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

All phases are built on branch `harsh-r/final-submission` (Phase 1 by Himank, EDA and the first forecast by Krish, the rest by Harsh Rohila). Merge into `main` is pending review by Krish. Read [progress](PROGRESS.md), [decisions](docs/decisions.md) and the [Phase 1 handover](docs/phase1_handover.md). One correction to the handover: the released `zone_neighbours` table was incomplete (692 edges); the corrected table has 1,302 edges and is rebuilt by `notebooks/rebuild_zone_neighbours.py`.

### Results in one table

All simulation results are on November–December 2024, a period the team had already examined, so they are not a pristine test.

| Hypothesis | Result |
|---|---|
| H1: request demand differs across pickup zones and hours | Supported (exploratory) |
| H2: a data-chosen launch area earns more profit per car than a fixed Manhattan + Downtown Brooklyn area | Not supported with the proxy-chosen areas; supported in a post hoc follow-up with a simulator-chosen 84-zone area (H2b) |
| H3: profit-based placement beats ride-count placement | Not supported in the fixed area; supported in the chosen area |
| H4: a forecast-based plan beats "copy last week" | Inconclusive |
| H5: moving free cars to nearby busy zones beats staying | Not supported |

Drift check on January 2025: data drift did not fire; forecast error drift fired from the week of 30 December (holiday weeks). Details: [`docs/decisions.md`](docs/decisions.md), [`docs/final_report.md`](docs/final_report.md).

## Start here for teammates and AI assistants

The [shared handover](docs/phase1_handover.md) lists exact tables, schemas, units, coverage and limitations. Obtain the published versioned data bundle there. Do not restart ETL to begin downstream development.

```text
Continue ADS-taxifleet in my local clone on my assigned task branch. Read AGENTS.md, repo_instructions.md, README.md, PROGRESS.md and docs/phase1_handover.md first. Inspect my branch, commit, local paths and data version; obtain, extract and verify phase1-2024-v1 if absent. Explain the pipeline and relevant tables simply, then continue my assigned task using the finalized data without restarting ETL. Never push, only I push.
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

## Reproduce Phases 2–7 from the Phase 1 release

Phases 2–7 read the published release (not raw trips). Set up the analysis environment and run the steps in order from the repository root. Each step reads the release and the outputs of the steps before it, and writes only under `data/` (git-ignored).

```bash
python -m venv .venv-analysis
.venv-analysis/Scripts/python.exe -m pip install -r requirements-analysis.txt   # Windows; use bin/python on Linux/macOS

# Corrected neighbour table (needs the Phase 1 environment with pyshp, shapely, pyproj and pyarrow)
.venv/bin/python notebooks/rebuild_zone_neighbours.py --lookup taxi_zone_lookup.csv \
    --shapefile taxi_zones/taxi_zones.shp \
    --output data/processed/phase1/zone_neighbours-fixed-20261115/zone_neighbours

# Phase 3b forecast (executes the notebook; writes data/forecasts/)
jupyter nbconvert --to notebook --execute --inplace notebooks/03b_forecast_with_lags.ipynb
# Phase 4 simulator demo, Phase 5 launch areas, Phase 6 hypothesis tests, H2b follow-up, Phase 7 drift
python notebooks/04_simulator.py
python notebooks/05_launch_area.py            # about 9 minutes
python notebooks/06_hypothesis_tests.py       # about 3 minutes
python notebooks/06b_h2_simulated_area.py     # about 30 minutes (search), then the test
python notebooks/07_drift_check.py
```

The forecast service in `service/` has its own [README](service/README.md) with the Docker commands. Windows note: in the Python 3.14 environment, Windows Application Control blocked `pyarrow`, so the Phase 2–7 scripts read and write Parquet through DuckDB.

## Run the tests

New code (Phases 3b–7, report generator) runs in the analysis environment without Spark. `tests/conftest.py` needs PySpark, so skip it with `--noconftest`:

```bash
.venv-analysis/Scripts/python.exe -m pytest --noconftest tests/test_demand_features.py tests/test_fleet_sim.py \
    tests/test_launch_area.py tests/test_hypothesis_tests.py tests/test_h2_simulated_area.py \
    tests/test_drift_monitor.py tests/test_forecast_api.py tests/test_build_report_docx.py -q
ruff check .
```

The Phase 1 tests use Spark 4.0.1, which needs **Java 17 or 21** (not 26). The easiest route on Windows is Docker:

```bash
docker run --rm -v "$(pwd):/repo:ro" python:3.11-bookworm bash -c '
  apt-get update -qq && apt-get install -y -qq openjdk-17-jre-headless
  mkdir /work && cd /repo && tar --exclude=.git --exclude=data --exclude=.venv --exclude=.venv-analysis -cf - . | tar -xf - -C /work && cd /work
  pip install -q -r requirements.txt pandas scikit-learn scipy statsmodels matplotlib seaborn
  python -m pytest tests/test_trip_cleaning.py tests/test_trip_audit.py tests/test_phase1_runner.py \
      tests/test_phase1_sources.py tests/test_phase1_tables.py tests/test_phase1_time.py \
      tests/test_eda_notebook.py tests/test_forecast_notebook.py -q'
```

## Folders

| Folder | Contents |
|---|---|
| `docs/` | The plan, decisions, handover, final report (`final_report.md`, built into `Project_Report.docx`) and its figures. |
| `data/` | Raw and processed data, forecasts, results. Not committed. |
| `notebooks/` | Phase 1 notebooks and runner, EDA and forecast notebooks (02, 03, 03b), scripts for Phases 4–7 (04–07, 06b) and `rebuild_zone_neighbours.py`. |
| `src/` | Tested code: trip cleaning and Phase 1 tables, demand features, fleet simulator, launch-area search, hypothesis-test statistics, drift monitor. |
| `service/` | Small forecast API (FastAPI) with Dockerfile. |
| `tests/` | pytest tests for `src/`, `service/` and the report generator, on small synthetic data. |

## Contributing

Read [`repo_instructions.md`](repo_instructions.md) first.
