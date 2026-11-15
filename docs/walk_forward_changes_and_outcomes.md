# Walk-forward validation: what we changed and what it gave

For the whole team. Written 2026-11-16 on branch `krish/fixes` (started from `harsh-r/final-submission` at `7364918`, so it includes Harsh's Phases 3b–8). The research decision behind it is in `docs/decisions.md`, "Walk-forward validation instead of one Sep–Oct validation" (pending team review). Harsh's `03b` notebook and his forecast file are **not changed**; everything new is in new files.

## In five lines

1. The old validation period (September–October) was an easy, almost rain-free stretch, so it was a weak basis for choosing the forecast method.
2. We replaced it with seven walk-forward folds (April–October), each trained only on earlier months, with a choosing rule written before any score was seen.
3. The data passed every integrity check we ran.
4. The result: the chosen method is the **mean of the previous 4 weeks**, the same as before. The forecast did **not** get more accurate; the choice is now supported by seven months of evidence instead of about six weeks.
5. The gradient boosting model is only tied with the 4-week mean on normal days. It is much better on federal holidays and not better in rain.

## 1. Why we changed anything

The plan chose forecast settings on one validation period, September–October 2024, after training on January–August. A look at the Phase 1 `zone_hour` table (January–October) shows this period is a weak test:

- Demand level differs between months: per-day requests range from about 611,000 (August) to about 680,000 (March), roughly 11% from lowest to highest, while the weekday-by-hour shape stays almost the same (correlation 0.98–0.99 against August).
- Rain adds about 12% demand in the same month, weekday and hour, in almost every month.
- September had observed rain in 5.7% of its hours and October in 0.1% (one hour), so the validation period could not test rain at all.
- Only 8 federal-holiday days fall in January–October.

These are city totals and associations, not causal claims.

## 2. Is the data sound? (checks we ran)

Read-only checks on the Phase 1 release. They were one-off scripts and are not in the repo; the results are listed here so anyone can repeat them.

| Check | Result |
|---|---|
| All 1,183 shared files: size and SHA-256 against `file_manifest.json` | all match |
| Row count of all 28 tables against `table_index.json` | all match |
| `zone_hour` structure | 2,505,864 rows = 263 zones × 9,528 hours; no duplicate keys; no gaps in the hourly sequence; period labels match the split; no negative counts; nulls only in the 526 uncovered hours; zone ids match the TLC lookup; weather values in a sensible range |
| January recount from the raw TLC file (using the cleaning rules in `src/trip_cleaning.py`) | matches in 195,459 of 195,672 zone-hour cells; the 213 cells that differ (1,405 requests, 0.007%) are all on 31 January, where `zone_hour` is higher, as expected from the February guard file |

Limits: the recount reuses the repo's own cleaning rules, so it proves the aggregation, not that the rules are right. February to December were not recounted (the raw files were not available), and weather was not compared with its source.

## 3. What we changed

| What | Where |
|---|---|
| Seven walk-forward folds: train Jan–Mar → validate Apr, … train Jan–Sep → validate Oct. A fold never trains on a month at or after the month it is scored on. | `make_walk_forward_folds` in `src/demand_features.py` |
| A choosing rule written before any score: compare by WAPE; the best model wins only if its mean beats the best baseline AND it wins at least 5 of 7 folds; otherwise the best baseline wins; the worst fold is printed. | `choose_method_from_folds`, `MIN_FOLD_WINS` in `src/demand_features.py` |
| Nine new tests. | `tests/test_demand_features.py` |
| A new notebook: the folds, the rule, a refit on January–October, and the usual November–December and January 2025 outputs. It reads data with `pyarrow` and writes to `data/forecasts_walk_forward/`. | `notebooks/03c_forecast_walk_forward.ipynb` |
| October forecasts come from the model trained on January–September, because the January–October refit has already seen October. November–January forecasts come from the refit model. | notebook Steps 8–10 |
| The old Oct 1–15 count-bias scale was dropped (the folds replace it). | notebook, `docs/decisions.md` |

## 4. Results

### Seven folds (WAPE %, lower is better)

| Method | Apr | May | Jun | Jul | Aug | Sep | Oct | Mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hgb_poisson_large (best model) | 13.45 | 13.56 | 13.01 | 15.30 | 12.98 | 14.99 | 12.97 | 13.75 |
| Mean of previous 4 weeks (best baseline) | 13.17 | 13.59 | 13.20 | 15.60 | 13.62 | 14.77 | 12.13 | 13.72 |
| Frozen 28-day profile | 13.64 | 13.74 | 13.46 | 16.07 | 14.12 | 15.83 | 12.57 | 14.21 |
| Last week | 16.08 | 16.25 | 17.18 | 19.34 | 15.45 | 16.61 | 15.01 | 16.56 |
| Frozen 7-day profile | 16.47 | 17.16 | 19.96 | 18.72 | 15.78 | 18.84 | 16.17 | 17.59 |

The model beat the 4-week mean in 4 of 7 folds (May, June, July, August). The rule needs 5, so **the chosen method is the 4-week mean**. Its worst fold is July (15.60%).

### Rain and holidays (pooled over the seven folds, WAPE %)

| | 4-week mean | best model |
|---|---:|---:|
| No rain | 13.52 | 13.51 |
| Rain | 16.46 | 17.10 |
| Normal day | 13.33 | 13.52 |
| Federal holiday | 32.20 | 24.76 |

### November–December (already examined in earlier experiments, so diagnostic only, not a pristine test; MAE in requests per zone-hour)

| Method | MAE | WAPE % |
|---|---:|---:|
| Best model, refit on Jan–Oct | 18.86 | 17.92 |
| Mean of previous 4 weeks (chosen) | 19.21 | 18.25 |
| Frozen 28-day profile | 19.88 | 18.89 |
| Last week | 23.11 | 21.95 |

These were not used to choose anything. The choice was saved before November–December was read.

### Compared with the old split (Harsh's `03b`; numbers from `docs/decisions.md`, not rerun)

| | Old split | Walk-forward |
|---|---|---|
| Evidence used to choose | September, then 1–15 and 16–31 October | seven months, April–October |
| Chosen method | Mean of previous 4 weeks | Mean of previous 4 weeks |
| Nov–Dec MAE of the chosen method | 19.21 | 19.21 |
| Nov–Dec MAE of the best model | 18.93 | 18.86 |

## 5. What went well

- The data passed every integrity check, so later results rest on a sound base.
- We found a real weakness in the old validation period and replaced it with a design that only ever trains on the past.
- The choosing rule was written before any score and behaved as designed. It stopped us picking a model that was only tied with a simple rule.
- The same method was chosen on much more evidence, so the old decision is now better supported.
- We learned where the model helps and where it does not: it is much better on federal holidays and not better in rain.
- Existing work was left untouched, nothing was pushed, and the new code has tests.

## 6. What went badly or is still weak

- **The forecast did not get more accurate.** The chosen method and its Nov–Dec score are identical to before (19.21). The change improved how we choose, not what we predict.
- **The model is only tied with the 4-week mean** on normal days (13.75% against 13.72%). It also lost the last two folds before the test months (September and, most clearly, October: 12.97% against 12.13%).
- **Rain hours are still the weak spot.** Every method has about 17% error in rain against about 13.5% on dry hours, and none of them uses rain as an input. The model is slightly worse than the baseline there.
- **The test months were already examined.** Every Nov–Dec number is a diagnostic re-check, not a fresh test.
- **Rain is not yet a model input.** The observed-rain flag is used only to report rainy-hour scores. Whether to add rain as an input, and whether to drop the raw month number (the model has never seen November or December), is undecided.
- **Phases 4–7 and the report were not re-run.** They use Harsh's older forecast file. Switching them to the new file needs a re-run.
- **The earliest fold is small** (it trains only on January–March), so its score is noisy.
- **Dropping the count-bias scale was not tested** under the new design.
- **Environment difference:** `03c` reads data with `pyarrow` (the forecast environment has no `duckdb`, and `shap` is not installed, so it uses permutation importance). `03b` uses `duckdb`.
- **Still open from earlier:** the released neighbour table is incomplete (the corrected 651-pair table is not released), and the handover's "259,885,329 cleaned rows" means input rows.

## 7. Questions for the team

1. Do you approve the walk-forward design in `docs/decisions.md`?
2. Should Phases 4–7 and the report stay on Harsh's current forecast file, or be re-run on `data/forecasts_walk_forward/forecast_zone_hour.parquet`? The chosen method is the same, so the main change would be the model column. This is Harsh's call.
3. Do we test rain inputs inside the same folds (the rain flag, rainfall amount, and whether it rained on the lag days; the code already gives the model "was the lag day a holiday" flags, which probably explains the holiday gain)?
4. Does the plan or course handout require PySpark in the modelling step? Spark is used only for the Phase 1 ETL, and the plan PDF could not be searched here.

## 8. How to reproduce

You need the Phase 1 release extracted and the forecast environment (Python 3.12, `notebooks/requirements-forecast.txt`). From the repository root:

```bash
python -m pytest tests/test_demand_features.py --noconftest -q
```

Then open `notebooks/03c_forecast_walk_forward.ipynb` and use Restart & Run All. On a laptop it took about 9 minutes and wrote `data/forecasts_walk_forward/` (the choice, both models, the forecast file and a summary JSON; this folder is ignored by git).
