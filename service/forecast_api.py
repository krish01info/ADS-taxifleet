"""Phase 7: a small forecast web service (FastAPI) for the frozen Phase 3 demand model.

Where it fits: this is the "deployment" step. It loads the frozen model once, keeps a table of past
zone-hour request counts in memory, and answers two requests:
    GET  /health    is the service alive, which model and history does it hold?
    POST /forecast  {"zone_id": 161, "start_utc": "2025-01-15T12:00:00Z", "hours": 24}
                    returns the predicted number of requests for each of those zone-hours.

Only information from at least 7 days earlier is used. The model inputs are built by
src/demand_features.py (the same code as in training): demand enters only through the counts of the
same zone and New York clock hour 7, 14, 21 and 28 days before the target hour. Calendar and zone
location are known in advance. Counts of the last 7 days are never read.

What the service needs (three files, paths come from environment variables):
    FORECAST_MODEL_PATH    the frozen model (data/forecasts/frozen_model.joblib)
    FORECAST_CHOICE_PATH   frozen_choice.json: the model input columns and the count-bias scale
    FORECAST_HISTORY_PATH  a table of past zone-hour counts (service/sample_history.csv.gz)

Limits to know about: the history is a fixed snapshot (1 December 2024 to 31 January 2025). A real
deployment would need a live feed of finished-trip counts, delivered with the same cleaning rules
as Phase 1, and that feed would have to be complete up to 7 days before each requested hour.
Forecasts for hours whose lag days are missing from the history are refused (no lag at all) or
flagged (fewer than four lags).

To rebuild the sample history from the Phase 1 release (needs duckdb, run from the repo root):
    python -m service.forecast_api export-history
"""

import contextlib
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pandas.tseries.holiday import USFederalHolidayCalendar
from pydantic import BaseModel, Field

from src.demand_features import (
    CIVIL_KEY,
    LAG_COLUMNS,
    build_features,
    list_borough_columns,
    make_civil_hour_table,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = REPO_ROOT / "data/forecasts/frozen_model.joblib"
DEFAULT_CHOICE_PATH = REPO_ROOT / "data/forecasts/frozen_choice.json"
DEFAULT_HISTORY_PATH = REPO_ROOT / "service/sample_history.csv.gz"
MODEL_PATH_VARIABLE = "FORECAST_MODEL_PATH"
CHOICE_PATH_VARIABLE = "FORECAST_CHOICE_PATH"
HISTORY_PATH_VARIABLE = "FORECAST_HISTORY_PATH"

NEW_YORK_TIME_ZONE = "America/New_York"
MAX_HOURS_PER_REQUEST = 168
HISTORY_COLUMNS = ["zone_id", "event_date_nyc", "hour_nyc", "request_count", "is_federal_holiday",
                   "borough", "centroid_latitude", "centroid_longitude", "is_airport"]
ZONE_COLUMNS = ["zone_id", "borough", "centroid_latitude", "centroid_longitude", "is_airport"]
REQUIRED_CHOICE_KEYS = ["feature_columns", "count_bias_scale", "pred_model_uses_scale"]
FOUR_LAGS = len(LAG_COLUMNS)

SAMPLE_FIRST_DATE = "2024-12-01"
SAMPLE_LAST_DATE = "2025-01-31"
RELEASE_INDEX_PATH = "data/releases/phase1-2024-v1/metadata/table_index.json"
SAMPLE_QUERY_COLUMNS = ("zone_id, event_date_nyc, hour_nyc, period_label, request_count, "
                                "is_federal_holiday")


class ForecastRequest(BaseModel):
    """What a caller sends to POST /forecast."""

    zone_id: int
    start_utc: datetime
    hours: int = Field(ge=1, le=MAX_HOURS_PER_REQUEST)


def to_civil_date(values):
    """Turn dates into New York calendar dates at midnight, in one fixed datetime type."""
    return pd.to_datetime(values).dt.normalize().astype("datetime64[ns]")


def load_service_data(model_path, choice_path, history_path):
    """Load the model, its settings and the history table. Stops with a clear error if anything is off."""
    for path in (model_path, choice_path, history_path):
        if not Path(path).is_file():
            raise FileNotFoundError(f"Service file not found: {path}")
    choice = json.loads(Path(choice_path).read_text(encoding="utf-8"))
    missing_keys = [key for key in REQUIRED_CHOICE_KEYS if key not in choice]
    if missing_keys:
        raise ValueError(f"{choice_path} is missing keys: {missing_keys}")
    model = joblib.load(model_path)
    history = pd.read_csv(history_path, parse_dates=["event_date_nyc"])
    missing_columns = [name for name in HISTORY_COLUMNS if name not in history.columns]
    if missing_columns:
        raise ValueError(f"History table is missing columns: {missing_columns}")
    history["event_date_nyc"] = to_civil_date(history["event_date_nyc"])
    if history.duplicated(CIVIL_KEY).any():
        raise ValueError("History table has a repeated zone, date and hour.")
    zones = history[ZONE_COLUMNS].drop_duplicates().reset_index(drop=True)
    if zones["zone_id"].duplicated().any():
        raise ValueError("A zone has two different sets of attributes in the history table.")
    boroughs = sorted(zones["borough"].unique())
    expected_borough_columns = [name for name in choice["feature_columns"] if name.startswith("borough_")]
    if list_borough_columns(boroughs) != expected_borough_columns:
        raise ValueError("The boroughs in the history table do not match the model's borough inputs.")
    if model.n_features_in_ != len(choice["feature_columns"]):
        raise ValueError("The model and frozen_choice.json disagree on the number of inputs.")
    return {
        "model": model, "feature_columns": choice["feature_columns"],
        "count_scale": float(choice["count_bias_scale"]) if choice["pred_model_uses_scale"] else 1.0,
        "history": history, "zones": zones, "boroughs": boroughs, "model_file": Path(model_path).name,
    }


def make_target_hours(start_utc, hours):
    """Return one row per requested hour: its UTC start, New York date and New York clock hour.

    A naive start time is read as UTC. The start must be exactly on the hour.
    """
    start = pd.Timestamp(start_utc)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    if start != start.floor("h"):
        raise ValueError("start_utc must be exactly on the hour, for example 2025-01-15T12:00:00Z.")
    hour_starts_utc = pd.date_range(start, periods=hours, freq="h")
    local_times = hour_starts_utc.tz_convert(NEW_YORK_TIME_ZONE)
    return pd.DataFrame({
        "hour_start_utc": hour_starts_utc,
        "event_date_nyc": to_civil_date(pd.Series(local_times.normalize().tz_localize(None))),
        "hour_nyc": local_times.hour.to_numpy(),
    })


def list_federal_holiday_dates(first_date, last_date):
    """Return the federal holiday dates between two dates (calendar knowledge, known in advance)."""
    return set(USFederalHolidayCalendar().holidays(first_date, last_date))


def build_target_features(service_data, zone_id, target_hours):
    """Return the model inputs for each distinct New York date and hour of the target hours.

    The table given to build_features holds the zone's past counts plus the target rows with an
    empty count. Each lag is looked up exactly 7, 14, 21 or 28 days before the target hour, so no
    count from the last 7 days can reach a target row.
    """
    keys = target_hours[CIVIL_KEY[1:]].drop_duplicates().reset_index(drop=True)
    keys["zone_id"] = zone_id
    keys["target_position"] = np.arange(len(keys), dtype=float)
    first_date, last_date = keys["event_date_nyc"].min(), keys["event_date_nyc"].max()
    holidays = list_federal_holiday_dates(first_date, last_date)
    keys["request_count"] = np.nan
    keys["is_federal_holiday"] = keys["event_date_nyc"].isin(holidays)
    history = service_data["history"]
    in_window = ((history["zone_id"] == zone_id) & (history["event_date_nyc"] >= first_date - pd.Timedelta(days=28))
                 & (history["event_date_nyc"] <= last_date))
    past_rows = history.loc[in_window, [*CIVIL_KEY, "request_count", "is_federal_holiday"]]
    past_rows = past_rows.merge(keys[CIVIL_KEY], on=CIVIL_KEY, how="left", indicator=True)
    past_rows = past_rows[past_rows["_merge"] == "left_only"].drop(columns="_merge")
    combined = pd.concat([past_rows, keys[[*CIVIL_KEY, "request_count", "is_federal_holiday", "target_position"]]],
                         ignore_index=True)
    combined["period_label"] = "forecast"
    with contextlib.redirect_stdout(io.StringIO()):
        features = build_features(combined, service_data["zones"], service_data["boroughs"])
    target_features = features[features["target_position"].notna()].sort_values("target_position")
    return target_features.reset_index(drop=True)


def predict_target_hours(service_data, zone_id, target_hours):
    """Return one forecast row per requested hour (model forecast, 4-week-mean reference, lags used)."""
    features = build_target_features(service_data, zone_id, target_hours)
    lags_available = features[LAG_COLUMNS].notna().sum(axis=1)
    if (lags_available == 0).any():
        raise ValueError("The history table has no counts 7, 14, 21 or 28 days before some requested hours.")
    raw_prediction = np.maximum(service_data["model"].predict(features[service_data["feature_columns"]]), 0.0)
    keyed = features[CIVIL_KEY[1:]].copy()
    keyed["predicted_requests"] = raw_prediction * service_data["count_scale"]
    keyed["four_week_mean_reference"] = features[LAG_COLUMNS].mean(axis=1).where(lags_available == FOUR_LAGS)
    keyed["lags_available"] = lags_available
    return target_hours.merge(keyed, on=CIVIL_KEY[1:], how="left", validate="many_to_one")


def make_forecast_response(service_data, request):
    """Run one forecast request and return the answer as a plain dictionary."""
    zone_known = request.zone_id in set(service_data["zones"]["zone_id"])
    if not zone_known:
        raise HTTPException(status_code=404, detail=f"Unknown zone_id {request.zone_id}.")
    try:
        target_hours = make_target_hours(request.start_utc, request.hours)
        forecast = predict_target_hours(service_data, request.zone_id, target_hours)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    history_last_date = service_data["history"]["event_date_nyc"].max().date().isoformat()
    some_lags_missing = bool((forecast["lags_available"] < FOUR_LAGS).any())
    rows = []
    for row in forecast.itertuples():
        four_week_mean = None if pd.isna(row.four_week_mean_reference) else round(float(row.four_week_mean_reference), 2)
        rows.append({
            "hour_start_utc": row.hour_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "predicted_requests": round(float(row.predicted_requests), 2),
            "four_week_mean_reference": four_week_mean,
            "lags_available": int(row.lags_available),
        })
    return {
        "zone_id": request.zone_id, "model_file": service_data["model_file"],
        "history_last_date": history_last_date, "forecasts": rows,
        "warning": ("Some hours have fewer than 4 lag counts in the history; read them with care."
                    if some_lags_missing else None),
    }


def create_app(model_path, choice_path, history_path):
    """Build the web app. The model and history are loaded once, when the app is built."""
    service_data = load_service_data(model_path, choice_path, history_path)
    app = FastAPI(title="Robotaxi demand forecast (frozen model)")

    @app.get("/health")
    def get_health():
        """Say that the service is alive and what it holds."""
        history = service_data["history"]
        return {
            "status": "ok", "model_file": service_data["model_file"],
            "zones": len(service_data["zones"]), "feature_count": len(service_data["feature_columns"]),
            "history_first_date": history["event_date_nyc"].min().date().isoformat(),
            "history_last_date": history["event_date_nyc"].max().date().isoformat(),
        }

    @app.post("/forecast")
    def post_forecast(request: ForecastRequest):
        """Forecast requests per hour for one zone, using only counts from at least 7 days earlier."""
        return make_forecast_response(service_data, request)

    return app


def create_app_from_environment():
    """Build the app with file paths from the environment variables (defaults point into the repo)."""
    return create_app(
        os.environ.get(MODEL_PATH_VARIABLE, DEFAULT_MODEL_PATH),
        os.environ.get(CHOICE_PATH_VARIABLE, DEFAULT_CHOICE_PATH),
        os.environ.get(HISTORY_PATH_VARIABLE, DEFAULT_HISTORY_PATH),
    )


def export_sample_history(output_path=DEFAULT_HISTORY_PATH):
    """Write the sample history (one row per zone-hour, with zone attributes) from the Phase 1 release."""
    import duckdb  # only needed to build the sample; the container does not install it

    table_index = json.loads((REPO_ROOT / RELEASE_INDEX_PATH).read_text(encoding="utf-8"))
    connection = duckdb.connect()
    zone_hour_folder = REPO_ROOT / table_index["zone_hour"]["path"]
    zones_folder = REPO_ROOT / table_index["zones"]["path"]
    zone_hour = connection.execute(
        f"select {SAMPLE_QUERY_COLUMNS} from read_parquet('{zone_hour_folder.as_posix()}/**/*.parquet', "
        f"hive_partitioning=true) where event_date_nyc between '{SAMPLE_FIRST_DATE}' and '{SAMPLE_LAST_DATE}'"
    ).df()
    zones = connection.execute(
        f"select {', '.join(ZONE_COLUMNS)} from read_parquet('{zones_folder.as_posix()}/**/*.parquet')").df()
    civil = make_civil_hour_table(zone_hour)
    sample = civil.merge(zones, on="zone_id", how="left", validate="many_to_one")
    if sample["borough"].isna().any():
        raise ValueError("Some zone ids are not in the zones table.")
    sample = sample[HISTORY_COLUMNS].sort_values(CIVIL_KEY)
    sample["event_date_nyc"] = sample["event_date_nyc"].dt.strftime("%Y-%m-%d")
    sample.to_csv(output_path, index=False)
    print(f"Wrote {len(sample):,} rows to {output_path}")


if __name__ == "__main__":
    if sys.argv[1:] == ["export-history"]:
        export_sample_history()
    else:
        raise SystemExit("Usage: python -m service.forecast_api export-history")
