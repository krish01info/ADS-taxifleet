# BADIR map: where each stage lives in this project

The course guidelines ask every project to follow **BADIR**: **B**usiness question, **A**nalysis plan, **D**ata collection, **I**nsights, **R**ecommendations. This page says, for each stage, what we decided, which file shows it, and what to point at in the viva. The longer version is Table 2.4 in `docs/final_report.md` (section 2.7). Written 2026-11-16 on branch `krish/fixes2`.

## The stakeholder and the decisions

The stakeholder is the fleet operations team of a robotaxi operator planning a possible New York City launch. Uber and Lyft trips stand in for robotaxi demand. Main KPI: total profit.

| Decision | Question |
|---|---|
| D1 | Which zones form the launch area? |
| D2 | How many cars wait in each zone each hour? |
| D3 | Does a car freed in a quiet zone stay, or drive empty to a busier zone? |

## B: Business question

| What | Where |
|---|---|
| Stakeholder, D1 to D3, KPIs, constraints, hypotheses H1 to H5 | `docs/ADS_Pre-Submission_Summary_Robotaxis.pdf`; `docs/decisions.md` ("Design"); `docs/final_report.md` chapters 1 and 2 |
| H1 revised to a demand hypothesis (one common price for all areas) | `docs/decisions.md` ("Phase 2 proposal revision") |
| Viva points | Why this problem; what decision it supports; why trips are a proxy for robotaxi demand |

## A: Analysis plan

| What | Where |
|---|---|
| Time split: train January to August, validate September to October, test November to December, drift check January 2025 | `docs/decisions.md` ("Design") |
| Walk-forward validation inside January to October (seven folds) with a choosing rule fixed before any score | `docs/decisions.md` ("Walk-forward validation"); `src/demand_features.py` (`make_walk_forward_folds`, `choose_method_from_folds`); `notebooks/03c_forecast_walk_forward.ipynb`; `docs/walk_forward_changes_and_outcomes.md` |
| Baselines: copy last week, 4-week mean, ride-count plan, fixed Manhattan plus Downtown Brooklyn area, cars staying put | `docs/decisions.md`; `docs/final_report.md` chapter 5 |
| Tests: paired Wilcoxon on daily profit, Holm correction, verdict rule written before the run | `src/hypothesis_tests.py`; `notebooks/06_hypothesis_tests.py` |
| Costs and sensitivity runs (1.36 USD per mile, 2 USD per idle car-hour) | `docs/decisions.md` ("Design") |

## D: Data collection

| What | Where |
|---|---|
| Four sources: TLC high-volume trips, TLC zone lookup and shapefile, NOAA hourly weather, OPM holidays | `docs/phase1_handover.md`; `notebooks/01_sources.ipynb` |
| Spark cleaning and one table per zone and hour (the single source of truth) | `notebooks/01_etl.ipynb`; `notebooks/01_integrate.ipynb`; `src/trip_cleaning.py`; `src/phase1_sources.py`; `src/phase1_tables.py` |
| Checks: file hashes, row counts, a January recount from the raw file | `docs/walk_forward_changes_and_outcomes.md` (section 2) |
| Course minimums (at least 100,000 rows, at least 20 variables, no Kaggle, several sources) | `notebooks/02_eda_simple.ipynb` section 2 (data card) |
| Missing values, cleaning rules, provider bias | `notebooks/02_eda_simple.ipynb` sections 3 and 14 |

## I: Insights

| What | Where |
|---|---|
| EDA and revised H1 (demand differs across zones and hours): supported | `notebooks/02_eda_simple.ipynb` (Jan to Oct: outliers, rain, holidays, fairness, random against forecast placement) |
| Demand forecast: gradient boosting against baselines; the 4-week mean is chosen | `notebooks/03b_forecast_with_lags.ipynb`; `notebooks/03c_forecast_walk_forward.ipynb`; `src/demand_features.py` |
| Fleet simulator and launch-area search | `src/fleet_sim.py`; `src/launch_area.py`; `notebooks/04_simulator.py`; `notebooks/05_launch_area.py` |
| H2 to H5 and the post hoc H2b | `notebooks/06_hypothesis_tests.py`; `notebooks/06b_h2_simulated_area.py` |
| Drift check on January 2025 | `src/drift_monitor.py`; `notebooks/07_drift_check.py` |

Verdicts (previously examined November to December 2024, not a pristine test): H1 supported; H2 not supported (supported in the post hoc H2b follow-up); H3 not supported in the baseline area; H4 inconclusive; H5 not supported. Unsupported and inconclusive results are valid outcomes (`docs/final_report.md` section 7.2).

## R: Recommendations

| What | Where |
|---|---|
| Key findings and recommendations (the project instructions ask for 5 to 10) | `docs/final_report.md` section 7.10 and chapter 9 |
| Implementation roadmap | `docs/final_report.md` section 7.11 |
| Working demo: forecast service in Docker and local MLflow | `service/`; `notebooks/07_mlflow_tracking.py` |
| Limits and responsible AI (fairness, drift, assumptions) | `docs/final_report.md` sections 7.6, 7.12 and 7.13 |

## The three presentation stages

| Stage | Slides | Evidence in the repo |
|---|---|---|
| 1. Define the problem | Stakeholder; questions and hypotheses; data audit; integration plan; project plan | Pre-submission PDF; `docs/decisions.md`; `docs/phase1_handover.md` |
| 2. Validate the evidence | Data preparation; EDA; hypothesis testing; unsupported or inconclusive hypotheses; revised plan | `notebooks/02_eda_simple.ipynb`; `notebooks/06_hypothesis_tests.py`; `docs/decisions.md` (changes to the plan) |
| 3. Inform the decision | Final findings; implications; impact and limits; roadmap | `docs/final_report.md` chapters 7 and 9 |

## How an EDA finding is written

Every important EDA finding follows the project instructions: **Observation** (what the numbers show), **Inference** (what it probably means, with its limits), **Decision relevance** (what it changes for D1 to D3 or for the model). Notebook `02_eda_simple` collects them in one table in section 16.

## Not covered here

Item numbers and verdict text for H1 to H5 come from `docs/decisions.md` and `PROGRESS.md`. If the report changes, update this page too.
