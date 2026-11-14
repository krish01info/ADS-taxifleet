# Progress

What is done, what is next, and what to know before starting. Updated after each step.

Deadline for Himank's approved Phase 1 batch: **15 November 2026**. The repository's original semester date was 24 November.

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
