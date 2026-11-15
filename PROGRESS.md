# Progress

What is done, what is next, and what to know before starting. Updated after each step.

Deadline for Himank's approved Phase 1 batch: **15 November 2026**. The repository's original semester date was 24 November.

## Phase 1 correction: zone neighbours (2026-11-15, Harsh Rohila)

- The released `zone_neighbours` table was incomplete (346 pairs; exact `touches()` missed shapes that overlap by slivers). Corrected to 651 pairs (1,302 directed edges, 10 connected groups). Fix in `src/phase1_sources.py` (`find_zone_neighbours`), five new tests, details in `docs/decisions.md`. Use the corrected table in `data/processed/phase1/zone_neighbours-fixed-20261115/zone_neighbours` until a corrected release is published. The 692 below and in `docs/phase1_handover.md` is superseded.
## Walk-forward validation, steps 1–2 of 3 done (2026-11-16, Krish; branch `krish/fixes`)

- Decision (pending team review, see `docs/decisions.md`): choose forecast settings with seven walk-forward monthly folds inside January–October (train Jan–Mar → validate Apr, … train Jan–Sep → validate Oct) instead of one Sep–Oct validation. Test stays Nov–Dec (already examined, so a re-check); Jan 2025 stays drift-only.
- Done: `make_walk_forward_folds` and `choose_method_from_folds` in `src/demand_features.py` (24 tests in the file pass, ruff passes) and the new notebook `notebooks/03c_forecast_walk_forward.ipynb` (33 cells, 5 figures, fresh run in about 9 minutes, no cell errors). Harsh's `03b` is untouched; 03c writes to `data/forecasts_walk_forward/`.
- Result of the 7 folds (Apr–Oct, WAPE): best model `hgb_poisson_large` 13.75% against the 4-week mean 13.72%, so they are tied. The model beat the 4-week mean in only 4 of 7 folds (needs 5), so the pre-written rule chose **Mean of previous 4 weeks**. The model wins May–Aug and loses Apr, Sep and Oct. It is clearly better on federal holidays (24.8% against 32.2% pooled) and not better in rain.
- Nov–Dec (previously examined, diagnostic only): model refit on Jan–Oct MAE 18.86, 4-week mean 19.21, frozen 28-day profile 19.88, last week 23.11 requests per zone-hour. Independent recount from the saved forecast file matches.
- Next: step 3, test an observed-rain input and dropping the raw month number inside the same folds. Rewrite the decisions entry with Jan–Oct evidence only. Phases 4–7 still use Harsh's old forecast file and were not re-run.

## Final submission build (2026-11-16, Harsh Rohila; branch `harsh-r/final-submission`)

| Phase | What | Done by | Where to look |
|---|---|---|---|
| 2 | EDA and revised H1 (demand differs by zone and hour) | Krish | `notebooks/02_eda_h1.ipynb` |
| 3 | Forecast. First version (calendar only) by Krish; Phase 3b with 7/14/21/28-day lags, gradient boosting, baselines, SHAP | Krish, Harsh Rohila | `notebooks/03_forecast.ipynb`, `notebooks/03b_forecast_with_lags.ipynb`, `src/demand_features.py` |
| 4 | Fleet simulator (15-minute slots, six placement policies) | Harsh Rohila | `src/fleet_sim.py`, `notebooks/04_simulator.py` |
| 5 | Launch-area optimiser, bridge links, fixed baseline area | Harsh Rohila | `src/launch_area.py`, `notebooks/05_launch_area.py` |
| 6 | Tests of H2–H5 (paired Wilcoxon, pre-registered verdict rule, sensitivity, fairness) | Harsh Rohila | `src/hypothesis_tests.py`, `notebooks/06_hypothesis_tests.py` |
| 7 | Drift check on January 2025, small Docker forecast API, local MLflow | Harsh Rohila | `src/drift_monitor.py`, `notebooks/07_drift_check.py`, `service/` |
| 8 | Report in the official template (Word) | In progress | `docs/final_report.md`, `docs/build_report_docx.py` |

- Verdicts (previously examined Nov–Dec 2024, not a pristine test): H2 not supported; H3 not supported in the baseline area (supported in the chosen area); H4 inconclusive; H5 not supported; a post hoc follow-up (H2b, simulator-chosen 84-zone area) is supported at all three fleet sizes. Details and the reasons for every choice are in `docs/decisions.md` ("Final phases 3b–7").
- Independent checks of Phase 1 and Krish's numbers (raw-file recounts, hashes, weather join, travel-time reference) all matched except the neighbour table, now corrected.
- Tests: run with `pytest --noconftest` in the Python 3.14 analysis environment (`tests/conftest.py` needs Spark and `time.tzset`, so the Spark tests need Linux, Java 17/21 and Python 3.11).
- Not done: a pristine test month, a live data feed, enforcement of the minimum service level, bridge-aware matching in the simulator.

## Local branch prepared for user push (2026-11-15)

- Branch: `krish/eda-forecast-availability-fix`, based on Himank's Phase 1 work (`4278280`). Research decisions committed separately from notebook/test implementation.
- EDA and corrected forecast notebooks have executed outputs. 22 focused EDA/forecast tests and Ruff on new notebook/test files pass; prior Phase 1 lint/Spark limitations remain documented.
- Only notebooks, pinned requirements, tests and shared progress/decision docs are included. Downloaded data, saved models and private handoff notes remain excluded by existing ignore rules.
- User must run the final push. No push, merge or pull-request creation performed by the assistant.

## Availability correction completed locally (2026-11-15)

- Fixed `03_forecast.ipynb` to require only known calendar/geography inputs. No recent request counts, demand lags/rolling values, observed current weather, fare or realised duration inputs. Models can predict schedules with no trip outcomes supplied.
- Training references are frozen from final 7/28 training days and explicitly named last-training-week/four-training-week profiles. They do not use evaluation counts and are not the original rolling baselines. Original rolling comparisons require proven as-of data before operational claims.
- Retrained RF/GB on all 1,539,865 Jan-Aug covered zone-hours; no history warmup. Same chronological tuning/calibration/selection partitions. Validation chose **Frozen four-training-week profile**, with RF_depth16_raw the best learned model. Shared diagnostic rows: 384,769; missing targets 526.
- November-December was already examined: corrected scores are diagnostic re-evaluation, not a fresh pristine test. Profile MAE 22.5212; RF 23.9679; GB 39.3708 requests/zone-hour. The old 29% improvement below is superseded and does not apply to the corrected contract. No profit gain claimed.
- New active folder `data/models/phase3-known-ahead-bac7110ae53581cf/`, pointer `data/models/current_forecast.json`. Old model files/notebook/tests are preserved for audit with a superseded marker. Corrected notebook: 23 cells, 11 code, 5 figures, fresh-kernel exit 0, no errors/stderr.
- 22 focused EDA/forecast tests and Ruff on new notebooks/tests pass. Both learned artifacts and the frozen profile replay 6,312 forecasts without request counts; independent MAE/RMSE match every saved method. Receipt in new folder/validation.json.
- Exact next step: review corrected findings and plan validation on a truly untouched future period or further changes using training/validation only. SHAP, course/instructor alignment, deployment and fleet-profit testing remain separate. Jan 2025 stays drift-only. No commits/pushes.

## Superseded lag-model experiment — audit only (2026-11-15)

- User explicitly chose Random Forest and standard Gradient Boosting after course-handout review, then authorized implementation. Branch `krish/phase3-forecast` carries existing uncommitted EDA work. No commit/push.
- Built and executed `notebooks/03_forecast.ipynb`: 21 cells, 10 code cells in order, 5 saved figures, no cell errors/stderr; fresh-kernel process exit 0. Pinned additions: `notebooks/requirements-forecast.txt`.
- Models fitted on 1,362,077 usable Jan-Aug zone-hours after declared history warmup/DST exclusions. Two configurations per family; September chooses settings, Oct 1-15 fits count-bias factors, Oct 16-31 selects raw/scaled models or history; selection saved before Nov-Dec evaluation. Jan 2025 untouched.
- Frozen winner `RF_depth16_raw`. Same 376,879 usable test zone-hours: RF MAE 13.6679; GB MAE 15.0920; four-week mean 19.2607; last-week 23.1317 requests/zone-hour. RF has 29.04% lower MAE than the stronger historical baseline. No profit improvement claimed.
- Initial 263-indicator attempt was stopped for resource cost before any scores. Final location inputs are coordinates/airport flag plus borough indicators and zone-specific lag history; compact float32 input, bounded trees, one worker, seeded 20% per-tree subsamples. All eligible training rows supplied.
- Models, encoder, frozen settings, forecasts, tuning/calibration records and validation: `data/models/phase3-d48acce4fd8a78b9/`. 21 EDA/forecast tests pass; new notebook/tests pass Ruff. Saved models both replay 6,312 stored forecasts; independent MAE/RMSE checks match. Phase 1 source is unchanged; its existing lint/Spark environment issues remain.
- Retrospective prior-hour final eligible counts are assumed available; operational publication/eligibility latency is not proven. SHAP and live deployment remain follow-ups; no intervals or full Phase 3 completion claimed. Next review results and availability assumptions, then plan SHAP/deployment and the separate fleet simulation with agreed price rates.

## Phase 2 local implementation (2026-11-15)

- Implemented `notebooks/02_eda_h1.ipynb`: 29 cells (14 code), 12 saved figures, explanatory text, training coverage/provider diagnostics, temporal/spatial demand, journey summaries, strict historical benchmarks, weather/holidays, pickup/drop-off imbalance and revised demand H1. User/team review and commit remain pending; no forecasting model or fleet simulation implemented.
- Downloaded the Phase 1 release; archive SHA-256 matches the published handover and all 1,183 shared-file hashes verified. Raw data and Phase 1 code unchanged.
- Full fresh-kernel execution succeeded (runner exit 0); all 1,539,865 Jan-Aug training zone-hours, 5,855 real hours, 263 zones. Held-out outcomes excluded. 11 focused EDA tests and Ruff on new files pass. Full `ruff check .` has 48 findings confined to unchanged Phase 1 files; old Spark tests were not rerun. Receipt: `data/eda/phase2-training/validation.json`.
- Revised H1 is supported within the stated exploratory weekly design (33 complete weeks), with dependence/zone-size/proxy limitations; not a profit-superiority result. Top 10 zones have 13.33% of summed mean demand rates. Four-week historical MAE is 14.07 vs last-week 17.26 on 2,208 common June-Aug development hours; no final model selected.
- EDA runtime is `.venv/eda/Scripts/python.exe` (Python 3.12), with pinned `notebooks/requirements-eda.txt`. The earlier top-level `.venv` install was interrupted; use the working nested EDA interpreter. Programmatic Windows NotebookClient runs need a selector event-loop policy; interactive notebook cells have no platform-specific timezone calls.
- Small summaries are under ignored `data/eda/phase2-training/`. No commit or push. Exact next step: review notebook findings, approve the pending H1/pricing deviation with the team, choose common price rates and plan Phase 3 chronologically on a new task branch.

## Phase 2 planning decision (2026-11-15)

- User approved revised H1: request demand differs meaningfully across pickup zones and hours. Pending team review; see `docs/decisions.md` for the original-proposal deviation and rationale.
- Planned simulation uses common base charge + per-passenger-mile charge across all zones, with no time charge; rates remain undecided. Recorded fares are preserved, not used to justify an area-specific pricing advantage.
- Keep the existing inclusive 1-minute to 3-hour cleaning rule; the 10-minute cutoff will not replace it.
- Planning decision implemented in the notebook above after user authorization. Revised demand H1 uses zone-hour counts; the old trip-level fare-H1 requirement is superseded. H1/pricing proposal changes remain pending team review.

## Local exploratory check: 10-minute cutoff (2026-11-15)

- Hypothetical only: no cleaning rule changed. Scanned all 19,663,930 rows of the local January 2024 raw file in bounded PyArrow batches, reproducing current acceptance predicates without rerunning Spark.
- Accepted trips: current 60-10,800 seconds = 19,468,657; proposed 600-10,800 seconds = 14,119,641; additional removal = 5,349,016 (27.4750%). Both endpoints inclusive.
- Mapped-pickup, valid-fare subset for zone-based H1: 19,467,741 -> 14,119,038; removal 5,348,703 (27.4747%). This is not a formal H1 test.
- Full-year effects are unknown. Results: `data/inspection/january_duration_cutoff_10min.json`. Header/endpoint checks and count reconciliations passed.
- Created a Python 3.12 `.venv` and installed PyArrow 24.0.0 only via uv after ensurepip failed. No Spark or plotting environment installed. uv cache is under ignored `data/tool-cache/`.
- EDA prompt and subsequent implementation/dependency batch were approved; implementation is recorded above. Subsequent user decision: retain the existing 1-minute minimum; see Phase 2 planning decision above.

## Done

| Phase | What | Done by | Where to look |
|---|---|---|---|
| 1 | Reproducible preparation, integrated tables and verified handover; data published, merge pending | Himank | `docs/phase1_handover.md` |
| 0 | Pre-submission summary: problem, hypotheses H1–H5, data sources, methods, validation plan | Team | `docs/ADS_Pre-Submission_Summary_Robotaxis.pdf` |

When a phase is finished, add its row with who did it. The AI uses "Done by" to decide whether to test the next person (`repo_instructions.md` §3a).

- 2026-11-09: Repo set up: rules, this file, `docs/decisions.md`, README. No code yet.

## Phase 1 complete on branch — Himank, 15 November 2026

See [shared teammate handover](docs/phase1_handover.md). Data publication is complete; merge into main remains pending. Verified version `finalized-20261114T190620-6b22f8cb` has 2,505,864 zone-hours, 263 zones, 692 directed neighbour edges and 69,169 pairs per OD period; all 2024 months and separate January 2025 drift were processed with adjacent guard slices. Final status `verified_phase1`, blockers empty; notebook final cell and all saved footer checks passed. Matching receipt: 57 tests and Ruff passed.

- 259,885,329 cleaned rows reconcile, including guard slices; raw and January checkpoint preserved.
- 526 zone-hours have incomplete request coverage; 4,471 lack primary weather. Missing monetary/weather amounts remain explicit. Temporal unresolved event totals: request 59,615; pickup 61,531; drop-off 54,468. See shared dictionary and coverage table for exact units and null policies.
- Portable index/manifest/dictionary/audits: `data/releases/phase1-2024-v1/metadata/`. Final zone-hour directory: `data/processed/phase1/finalized-20261114T190620-6b22f8cb/zone_hour`; OD inputs resolve through the index to the earlier available-run directory.
- Published [release](https://github.com/krish01info/ADS-taxifleet/releases/tag/phase1-2024-v1): [archive](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/ads-taxifleet-phase1-2024-v1.tar.gz) and [SHA256SUMS](https://github.com/krish01info/ADS-taxifleet/releases/download/phase1-2024-v1/SHA256SUMS). Local bundle remains `data/releases/phase1-2024-v1/ads-taxifleet-phase1-2024-v1.tar.gz`, 355,388,882 bytes. Himank confirmed GitHub metadata shows both assets uploaded and archive size/SHA-256 matching the previously verified local archive (`fa96ebbf0a8663167036089582ced3cb4ce6bc311a57877875af374c04d45eea`). The release tag targets `64d8663ba5a2020b62e97a6fc558c02be00ac571`; no fresh download-and-hash verification of the hosted archive is claimed.
- Cleaning collision/chronology policies unchanged. Provider exclusions, completed-trip censoring, unresolved DST and unverified original report collision rate remain limitations. Downstream phases are not complete.

## Next — teammate/AI starting message

Assignments are not inferred. Begin your assigned downstream work after the repository's required understanding check:

```text
Continue ADS-taxifleet in my local clone on my assigned task branch. Read AGENTS.md, repo_instructions.md, README.md, PROGRESS.md and docs/phase1_handover.md first. Inspect my branch, commit, local paths and data version; obtain, extract and verify phase1-2024-v1 if absent. Explain the pipeline and relevant tables simply, complete required understanding checks, then continue my assigned task using the finalized data without restarting ETL. Never push, only I push.
```

## Phases after that

| Phase | What | Owner |
|---|---|---|
| 2 | EDA of trip value by zone and hour; test H1 | — |
| 3 | Demand forecast: gradient boosting, tuning, calibration, SHAP | — |
| 4 | Fleet simulator: hourly placement, 15-minute matching, three fleet sizes | — |
| 5 | Launch-area optimisation (connected set of zones) | — |
| 6 | Test H2–H5 on the test months (paired Wilcoxon on daily profit) | — |
| 7 | Deployment and drift check on January 2025. How to deploy is not fixed yet (the plan says a forecast API in Docker and MLflow; may change) | — |
| 8 | Report and public GitHub repo | — |
