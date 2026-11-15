# Forecast service (Phase 7)

A small web service for the frozen Phase 3 demand model (`data/forecasts/frozen_model.joblib`).
It answers: "how many requests will zone Z get in the next H hours?"

Only counts from at least 7 days before each requested hour are used. The model inputs are built
by `src/demand_features.py` (the same code as in training): the count of the same zone and New York
clock hour 7, 14, 21 and 28 days earlier, plus calendar and zone location. The last 7 days of counts
are never read.

## Endpoints

| Request | What it does |
|---|---|
| `GET /health` | `status: ok`, the model file, number of zones, the first and last date of the history held |
| `POST /forecast` | body `{"zone_id": 161, "start_utc": "2025-01-15T12:00:00Z", "hours": 24}` (hours 1 to 168, start exactly on the hour; a start time without a time zone is read as UTC) |

`/forecast` returns one row per hour with `predicted_requests` (frozen model, with the saved count-bias
scale), `four_week_mean_reference` (mean of the four lag counts, the baseline the team picked on
16-31 October; null unless all four lags exist) and `lags_available` (0 to 4). Errors: unknown zone = 404;
bad input, a start not on the hour, or hours with no lag counts in the history = 422. If some hours have
fewer than 4 lags the answer carries a `warning`.

## The history table (limits)

The service needs past zone-hour counts. `service/sample_history.csv.gz` (about 1.7 MB) is a fixed
snapshot of the Phase 1 release: every zone and hour from 1 December 2024 to 31 January 2025, with the
zone attributes. Forecasts for 1 January 2025 onward therefore have all four lags, and forecasts for
hours more than about 28 days after 31 January have none and are refused. This is a demo, not a live
system: a real deployment would need a live feed of finished-trip counts, cleaned by the Phase 1 rules and
complete up to 7 days before each requested hour. The service does not retrain or update the model.

Rebuild the sample from the release (repo root, needs duckdb):

    python -m service.forecast_api export-history

## Run it without Docker (repo root)

    pip install -r service/requirements.txt
    uvicorn --factory service.forecast_api:create_app_from_environment --port 8000

The file paths come from the environment variables `FORECAST_MODEL_PATH`, `FORECAST_CHOICE_PATH`
and `FORECAST_HISTORY_PATH` (defaults point into the repo).

## Build and run with Docker (Git Bash, repo root)

The repo's `data/` folder is several GB, so the image is built from a small staging folder inside
`data/` (git-ignored), not from the repo root:

    rm -rf data/service_build && mkdir -p data/service_build/src data/service_build/model
    cp service/forecast_api.py service/requirements.txt service/Dockerfile service/sample_history.csv.gz data/service_build/
    cp src/demand_features.py data/service_build/src/
    cp data/forecasts/frozen_model.joblib data/forecasts/frozen_choice.json data/service_build/model/
    docker build -t robotaxi-forecast-api data/service_build
    docker run -d --name robotaxi-forecast -p 8000:8000 robotaxi-forecast-api

Check it:

    curl http://localhost:8000/health
    curl -X POST http://localhost:8000/forecast -H "Content-Type: application/json" \
         -d '{"zone_id": 161, "start_utc": "2025-01-15T12:00:00Z", "hours": 3}'

Stop it: `docker stop robotaxi-forecast && docker rm robotaxi-forecast`.

If the model file is refreshed by the forecast notebook, copy the staging files again and rebuild.
The image pins the same package versions the model was saved and tested with (`requirements.txt`);
a different scikit-learn version may refuse or mis-read the model file.

## Tests

    python -m pytest --noconftest tests/test_forecast_api.py -q

The tests need `httpx` (FastAPI's test client) and use an invented model and history, plus one check
against `data/forecasts/forecast_zone_hour.parquet` that is skipped when that file is not present.
