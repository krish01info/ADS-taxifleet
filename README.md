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

Phase 0 (pre-submission summary) is done. Next is Phase 1: the Spark ETL and SSOT tables.
See [`PROGRESS.md`](PROGRESS.md).

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
