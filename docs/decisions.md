# Decisions

Everything the team has decided, in order. Details and reasons are in the plan (`ADS_Pre-Submission_Summary_Robotaxis.pdf`).

## Design (pre-submission, 23 October 2026)

- Stakeholder: the fleet operations team of a robotaxi operator (such as Waymo) planning a possible New York City launch. Riders and the city regulator are secondary stakeholders.
- Decisions the project supports: which zones form the launch area; how many cars wait in each zone each hour; whether a car freed in a quiet zone stays or drives empty to a busier one.
- Main KPI: total profit. Others: profit per car per day, empty miles as a share of all miles, idle car-hours, share of requests served, gain over "copy last week", service gap between low- and high-demand zones.
- Hypotheses: H1 is tested on real trips; H2–H5 are tested in the fleet simulation on the test months.
- Data: NYC TLC High-Volume FHV trips for January–December 2024 (Uber/Lyft trips stand in for robotaxi demand), TLC zone lookup and shapefile (263 zones), NOAA hourly weather (Central Park; LaGuardia and JFK for checks), US OPM federal holidays. January 2025 is used only for the drift check.
- Not used: Waymo's public trip reports (location, time and mileage are redacted), Census income, NYC events data.
- Revenue = base passenger fare. Tips are excluded, because riders may not tip a robotaxi and tips are known only after the trip.
- Costs = recorded tolls + cost per mile × all miles + idle cost. $1.36 per mile (tested at $0.74 and $2.00); $2 per idle car-hour (tested at $0 and $5).
- Empty trips use real-trip median miles and minutes between zones; straight-line distance for zone pairs with few trips.
- Trip filters: duration 1 minute to 3 hours, speed 1–80 mph, request before pickup. Trips that share a key are flagged, not dropped. Row counts are checked after every join.
- Time split: train January–August 2024, validate September–October, test November–December. Holiday days are reported separately.
- Baselines: "copy last week", a ride-count plan, a fixed Manhattan plus Downtown Brooklyn area, and cars staying put.
- Plans are compared on the same test days with paired Wilcoxon tests on daily profit. A minimum service level applies to every zone.
- Each hypothesis is classed as supported, not supported or inconclusive.

## Repo (November 2026)

- 2026-11-09: Repo rules (`repo_instructions.md`), `PROGRESS.md` and this file added, following the layout of the costAware project. Krish (`krish01info`) is the only one who merges into `main`. AI assistants never push.
- 2026-11-09: Data is never committed. It lives in `data/`, which git ignores, and every table must be rebuildable from the raw files by the repo's notebooks and code.
- 2026-11-09: Code rules added to `repo_instructions.md` §7: easy to understand and explain, sensible names, no sloppy code. Reason (Krish): the code must be understandable and explainable, and function names must be sensible and correct, not random.
- 2026-11-09: `ruff` (a code checker) is added in Phase 1, with the first code. `ruff check .` must pass before every pull request. Reason: written rules depend on people following them; `ruff` catches unused imports, undefined names and messy code automatically. Agreed by Krish. Every teammate's AI explains what `ruff` is and why it was added the first time it comes up (`repo_instructions.md` §7, "Tools the team agreed on").
- 2026-11-09: Work is done in Jupyter notebooks (`notebooks/`) wherever it can be. Only the most important functions (trip cleaning filters, cost and profit calculation, empty-trip miles) go in a small module in `src/`, which the notebooks import and pytest tests. Reason (Krish): what can be done in the `.ipynb` should be in it; the key functions need a `.py` file so they can be tested.
- 2026-11-09: How to deploy (Phase 7) is not fixed. The plan's forecasting API in Docker may change. Reason (Krish): "it is not fixed that we gonna use the forecast API and Docker, we can change the plan." Pending team review. If it changes, the final report must say so, because the pre-submission summary said no deviations were planned.
