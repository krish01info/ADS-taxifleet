# Progress

What is done, what is next, and what to know before starting. Updated after each step.

Deadline: the semester ends on **24 November 2026**.

## Done

| Phase | What | Done by | Where to look |
|---|---|---|---|
| 0 | Pre-submission summary: problem, hypotheses H1–H5, data sources, methods, validation plan | Team | `docs/ADS_Pre-Submission_Summary_Robotaxis.pdf` |

When a phase is finished, add its row with who did it. The AI uses "Done by" to decide whether to test the next person (`repo_instructions.md` §3a).

- 2026-11-09: Repo set up: rules, this file, `docs/decisions.md`, README. No code yet.

## Next: Phase 1 — Spark ETL and SSOT tables

### Trip-cleaning step complete for January (Himank, `himank/phase1-etl`, 2026-11-14)

- Raw January trips and zone lookup/boundaries are present in gitignored `data/raw/`; the footer gives 19,663,930 trips. Initial inspection is in `data/inspection/january_2024_initial_inspection.json` (local only).
- Cleaning module, synthetic tests and notebook are implemented in `src/trip_cleaning.py`, `tests/test_trip_cleaning.py` and `notebooks/01_etl.ipynb`. The agreed eligibility/collision policies are in `docs/decisions.md` (2026-11-14).
- Runtime: Python 3.11.9, Java 21.0.11, Spark 4.0.1, Py4J 0.10.9.9, Ruff 0.14.4. Exact package versions are declared in `requirements.txt` and recorded locally in `data/inspection/cleaning_runtime_versions.json`.
- Validation: 24 synthetic tests and Ruff passed. A 10,000-row real-data sample reconciled (9,921 accepted, 79 quarantined); an independent bounded comparison preserved every original field and record multiplicity. The assistant's sandbox blocks local Spark sockets, so Himank ran the notebook runner in his normal terminal. Both sample-only and January runs completed in fresh kernels, from first cell to last, with raw hashes unchanged.
- January: 19,468,657 accepted + 195,273 quarantined = all 19,663,930 input records. Independent, overlapping eligibility counts: demand 19,467,915; simulation 18,738,600; fare analysis and monetary analysis 19,468,483 each.
- January's sequential quarantine waterfall: 2 nonpositive durations, 2,429 further out-of-range durations, 5,739 further speed failures, 187,103 further request-order failures. Individual reason flags overlap; see the audit rather than summing them.
- One repeated-key group contains 2 records, both retained. There are 9,790 timestamp-duration disagreements, all -1 second (timestamp duration minus `trip_time`). There are 191 negative base fares (174 in accepted trips), and 137 negative driver-pay values; none were changed to zero. Optional originating-base/on-scene fields each have 5,218,737 nulls; essential cleaning fields have none.
- Read-only request-order review (2026-11-14): the 187,103 sequential failures split into 93 equal timestamps and 187,010 requests after pickup. Uber/HV0003: 90 equal, 93,464 after; Lyft/HV0005: 3 equal, 93,546 after. Another 1,241 request-order-flagged rows already failed earlier checks (188,344 flagged overall). Signed pickup-minus-request medians: Uber -679 seconds, Lyft -900 seconds. Full distributions are local in `data/inspection/request_order_review-20261114T143302809636Z.json` and its aggregate histogram CSV.
- Both collision records are quarantined Lyft rows with matching six-field keys, but different miles, base fares, Black Car Fund amounts and sales tax. They are not identical full records. No collision-key or chronology rule was changed during review; original report comparisons still need the original audit definitions.
- Review accepted by Himank: keep chronology and collision policies unchanged. Chronology-stage exclusion shares are 0.6482% for Uber and 1.7883% for Lyft, using all January input trips for each provider as denominators. Record provider-specific exclusions as a demand-proxy limitation. During zone-hour work, check concentration using counts and rates by provider/zone/hour, comparing recorded request and pickup hours explicitly; no guessed repairs. This follow-up is deferred, not implemented here. The cleaning-step changes are ready for checkpoint review; no commit or push is authorised.
- Local January audit: `data/audits/trip_cleaning/january-2024-20261114T142109-e471127c.json`; output: `data/processed/trip_cleaning/january-2024-20261114T142109-e471127c/trips/`, partitioned into accepted/quarantined. Both are gitignored. The saved `notebooks/01_etl.ipynb` contains the successful full-run output. No commits or pushes were made.
- Stop after trip cleaning. The remaining Phase 1 stages below are not implemented. The pre-submission report's original/full-year audit claims remain unverified.

From the plan (§4, §5):

- Clean the trips: one row per trip. Filters: duration 1 minute to 3 hours, speed 1–80 mph, request before pickup. Flag (don't drop) the 0.08–0.09% of trips that share a key.
- Build the zone-hour table: one row per zone and hour (requests, drop-offs, fares, miles, weather, holiday).
- Build the zone-pair table: one row per zone pair (trip counts, median miles and minutes), used for empty-trip travel.
- Joins: zone IDs to the zone table, request hour (local standard time) to Central Park weather, date to holidays, neighbouring zones from the shapefile. Print row counts after every join.
- Set up the folders: the ETL goes in `notebooks/01_etl.ipynb`; the trip cleaning filters go in a small module in `src/`, tested with pytest on a small sample in `tests/` (decided 2026-11-09).
- Add `ruff` to `requirements.txt`; `ruff check .` must pass before every pull request (decided 2026-11-09, see `docs/decisions.md`).
- Owner and branch for the trip-cleaning step: Himank, `himank/phase1-etl`.

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
