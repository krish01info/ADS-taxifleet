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

From the plan (§4, §5):

- Clean the trips: one row per trip. Filters: duration 1 minute to 3 hours, speed 1–80 mph, request before pickup. Flag (don't drop) the 0.08–0.09% of trips that share a key.
- Build the zone-hour table: one row per zone and hour (requests, drop-offs, fares, miles, weather, holiday).
- Build the zone-pair table: one row per zone pair (trip counts, median miles and minutes), used for empty-trip travel.
- Joins: zone IDs to the zone table, request hour (local standard time) to Central Park weather, date to holidays, neighbouring zones from the shapefile. Print row counts after every join.
- Set up the folders: the ETL goes in `notebooks/01_etl.ipynb`; the trip cleaning filters go in a small module in `src/`, tested with pytest on a small sample in `tests/` (decided 2026-11-09).
- Add `ruff` to `requirements.txt`; `ruff check .` must pass before every pull request (decided 2026-11-09, see `docs/decisions.md`).
- Owner and branch: not decided yet. Ask the user.

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
