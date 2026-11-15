"""Tests for service/forecast_api.py with FastAPI's TestClient.

Most tests build a tiny invented model and history (two zones, seeded random counts) so they run
anywhere. The last test compares the service with the team's forecast file and is skipped when the
real data files are not on this machine.
"""

import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import forecast_api
from src.demand_features import (
    build_features,
    list_feature_columns,
    make_civil_hour_table,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RANDOM_SEED = 5
FIRST_HISTORY_DATE = "2024-09-01"
HISTORY_DAYS = 122
ZONE_IDS = [1, 2]
BOROUGHS = ["Bronx", "Queens"]
COUNT_SCALE = 1.1
TARGET_START = "2024-12-20T05:00:00Z"
TARGET_DATE = pd.Timestamp("2024-12-20")
DAYS_PER_WEEK = 7
REAL_ZONE_ID = 161
REAL_START = "2025-01-15T05:00:00Z"
REAL_DATE = "2025-01-15"
REAL_HOURS = 24
REAL_TOLERANCE = 0.01


def make_zones():
    """Make a two-zone zones table with the columns the service needs."""
    return pd.DataFrame({
        "zone_id": ZONE_IDS, "borough": BOROUGHS,
        "centroid_latitude": [40.8, 40.7], "centroid_longitude": [-73.9, -73.8],
        "is_airport": [False, True],
    })


def make_history(seed=RANDOM_SEED):
    """Make invented hourly counts for two zones with the zone attributes, in the history file layout."""
    random_generator = np.random.default_rng(seed)
    dates = pd.date_range(FIRST_HISTORY_DATE, periods=HISTORY_DAYS)
    rows = pd.MultiIndex.from_product([ZONE_IDS, dates, range(24)],
                                      names=["zone_id", "event_date_nyc", "hour_nyc"]).to_frame(index=False)
    rows["request_count"] = random_generator.integers(0, 300, size=len(rows)).astype(float)
    rows["is_federal_holiday"] = False
    return rows.merge(make_zones(), on="zone_id", how="left")


def write_service_files(folder, history, count_scale=COUNT_SCALE, uses_scale=True):
    """Fit a tiny model on the invented history and write the model, choice and history files."""
    zone_hour = history[["zone_id", "event_date_nyc", "hour_nyc", "request_count", "is_federal_holiday"]].copy()
    zone_hour["period_label"] = "train"
    features = build_features(make_civil_hour_table(zone_hour), make_zones(), BOROUGHS)
    feature_columns = list_feature_columns(BOROUGHS)
    model = HistGradientBoostingRegressor(max_iter=20, random_state=RANDOM_SEED)
    known = features[features["request_count"].notna()]
    model.fit(known[feature_columns], known["request_count"])
    paths = {"model": folder / "model.joblib", "choice": folder / "choice.json", "history": folder / "history.csv.gz"}
    joblib.dump(model, paths["model"])
    paths["choice"].write_text(json.dumps({"feature_columns": feature_columns, "count_bias_scale": count_scale,
                                           "pred_model_uses_scale": uses_scale}), encoding="utf-8")
    history.assign(event_date_nyc=history["event_date_nyc"].dt.strftime("%Y-%m-%d")).to_csv(paths["history"], index=False)
    return paths


def swap_history(paths, new_history, folder):
    """Return the same model and choice files with a different history file written into the folder."""
    history_path = folder / "other_history.csv.gz"
    new_history.assign(event_date_nyc=new_history["event_date_nyc"].dt.strftime("%Y-%m-%d")).to_csv(
        history_path, index=False)
    return {**paths, "history": history_path}


def make_client(paths):
    """Build the app from the three files and wrap it in a test client."""
    return TestClient(forecast_api.create_app(paths["model"], paths["choice"], paths["history"]))


def ask_forecast(client, zone_id=1, start_utc=TARGET_START, hours=24):
    """Send one forecast request and return the response."""
    return client.post("/forecast", json={"zone_id": zone_id, "start_utc": start_utc, "hours": hours})


@pytest.fixture(scope="module")
def service_files(tmp_path_factory):
    """Write the invented service files once for the whole test module."""
    return write_service_files(tmp_path_factory.mktemp("service_files"), make_history())


@pytest.fixture(scope="module")
def client(service_files):
    """A test client on the invented model and history."""
    return make_client(service_files)


def test_health_reports_model_history_and_zones(client):
    answer = client.get("/health")
    assert answer.status_code == 200
    body = answer.json()
    assert body["status"] == "ok"
    assert body["zones"] == len(ZONE_IDS)
    assert body["feature_count"] == len(list_feature_columns(BOROUGHS))
    assert body["history_first_date"] == FIRST_HISTORY_DATE


def test_forecast_returns_one_non_negative_row_per_hour(client):
    answer = ask_forecast(client, hours=24)
    assert answer.status_code == 200
    rows = answer.json()["forecasts"]
    assert len(rows) == 24
    assert rows[0]["hour_start_utc"] == TARGET_START
    assert rows[1]["hour_start_utc"] == "2024-12-20T06:00:00Z"
    assert all(row["predicted_requests"] >= 0 for row in rows)
    assert all(row["lags_available"] == 4 for row in rows)
    assert answer.json()["warning"] is None


def test_naive_start_time_is_read_as_utc(client):
    with_z = ask_forecast(client, start_utc=TARGET_START, hours=3).json()["forecasts"]
    naive = ask_forecast(client, start_utc="2024-12-20T05:00:00", hours=3).json()["forecasts"]
    assert with_z == naive


def test_count_scale_from_the_choice_file_is_applied(tmp_path_factory, client):
    unscaled_files = write_service_files(tmp_path_factory.mktemp("unscaled"), make_history(), uses_scale=False)
    unscaled = ask_forecast(make_client(unscaled_files), hours=24).json()["forecasts"]
    scaled = ask_forecast(client, hours=24).json()["forecasts"]
    ratios = [scaled_row["predicted_requests"] / plain_row["predicted_requests"]
              for scaled_row, plain_row in zip(scaled, unscaled, strict=True) if plain_row["predicted_requests"] > 1]
    assert ratios
    assert ratios == pytest.approx([COUNT_SCALE] * len(ratios), rel=0.01)


def test_four_week_mean_reference_is_the_mean_of_the_four_lag_counts(client):
    history = make_history().set_index(["zone_id", "event_date_nyc", "hour_nyc"])["request_count"]
    first_row = ask_forecast(client, zone_id=2, hours=1).json()["forecasts"][0]
    lag_counts = [history.loc[(2, TARGET_DATE - pd.Timedelta(days=DAYS_PER_WEEK * weeks), 0)] for weeks in (1, 2, 3, 4)]
    assert first_row["four_week_mean_reference"] == pytest.approx(np.mean(lag_counts), abs=0.01)


def test_counts_from_the_last_7_days_never_change_the_forecast(service_files, tmp_path, client):
    baseline = ask_forecast(client, hours=24).json()["forecasts"]
    history = make_history()
    last_lag_day = TARGET_DATE - pd.Timedelta(days=DAYS_PER_WEEK)
    history.loc[history["event_date_nyc"] > last_lag_day, "request_count"] = 9999.0
    changed_client = make_client(swap_history(service_files, history, tmp_path))
    assert ask_forecast(changed_client, hours=24).json()["forecasts"] == baseline


def test_a_lag_day_count_does_change_the_forecast(service_files, tmp_path, client):
    baseline = ask_forecast(client, hours=24).json()["forecasts"]
    history = make_history()
    lag_day = TARGET_DATE - pd.Timedelta(days=DAYS_PER_WEEK)
    history.loc[(history["event_date_nyc"] == lag_day) & (history["zone_id"] == 1), "request_count"] = 9999.0
    changed_client = make_client(swap_history(service_files, history, tmp_path))
    after_change = ask_forecast(changed_client, hours=24).json()["forecasts"]
    assert after_change != baseline
    assert after_change[0]["four_week_mean_reference"] > baseline[0]["four_week_mean_reference"]


def test_clock_change_hours_get_the_same_new_york_hour_and_the_same_forecast(client):
    rows = ask_forecast(client, start_utc="2024-11-03T04:00:00Z", hours=4).json()["forecasts"]
    assert len(rows) == 4
    assert rows[1]["predicted_requests"] == rows[2]["predicted_requests"]


def test_bad_requests_are_refused_with_a_clear_status(client):
    assert ask_forecast(client, zone_id=999).status_code == 404
    assert ask_forecast(client, hours=0).status_code == 422
    assert ask_forecast(client, hours=forecast_api.MAX_HOURS_PER_REQUEST + 1).status_code == 422
    not_on_the_hour = ask_forecast(client, start_utc="2024-12-20T05:30:00Z")
    assert not_on_the_hour.status_code == 422
    assert "on the hour" in not_on_the_hour.json()["detail"]
    assert client.post("/forecast", json={"zone_id": 1}).status_code == 422
    assert ask_forecast(client, start_utc="not a time").status_code == 422


def test_hours_beyond_the_history_are_refused_not_guessed(client):
    answer = ask_forecast(client, start_utc="2025-03-01T05:00:00Z", hours=3)
    assert answer.status_code == 422
    assert "no counts" in answer.json()["detail"]


def test_partly_missing_lags_are_flagged(client):
    start_with_only_some_lags = "2024-09-20T04:00:00Z"
    answer = ask_forecast(client, start_utc=start_with_only_some_lags, hours=2)
    assert answer.status_code == 200
    assert answer.json()["warning"] is not None
    assert answer.json()["forecasts"][0]["lags_available"] < 4


def test_loading_fails_loudly_on_missing_or_inconsistent_files(service_files, tmp_path):
    with pytest.raises(FileNotFoundError):
        forecast_api.create_app(tmp_path / "missing.joblib", service_files["choice"], service_files["history"])
    broken_choice = json.loads(service_files["choice"].read_text(encoding="utf-8"))
    broken_choice["feature_columns"] = broken_choice["feature_columns"][:-1]
    broken_path = tmp_path / "broken_choice.json"
    broken_path.write_text(json.dumps(broken_choice), encoding="utf-8")
    with pytest.raises(ValueError):
        forecast_api.create_app(service_files["model"], broken_path, service_files["history"])
    broken_path.write_text(json.dumps({"feature_columns": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        forecast_api.create_app(service_files["model"], broken_path, service_files["history"])


def test_service_matches_the_teams_forecast_file_on_real_data():
    duckdb = pytest.importorskip("duckdb")
    forecast_path = REPO_ROOT / "data/forecasts/forecast_zone_hour.parquet"
    real_files = {"model": forecast_api.DEFAULT_MODEL_PATH, "choice": forecast_api.DEFAULT_CHOICE_PATH,
                  "history": forecast_api.DEFAULT_HISTORY_PATH}
    if not forecast_path.is_file() or not all(path.is_file() for path in real_files.values()):
        pytest.skip("Real forecast files are not on this machine.")
    expected = duckdb.connect().execute(
        f"select hour_nyc, pred_model from read_parquet('{forecast_path.as_posix()}') "
        f"where zone_id = {REAL_ZONE_ID} and event_date_nyc = '{REAL_DATE}' order by hour_nyc").df()
    rows = ask_forecast(make_client(real_files), zone_id=REAL_ZONE_ID, start_utc=REAL_START,
                        hours=REAL_HOURS).json()["forecasts"]
    served = [row["predicted_requests"] for row in rows]
    assert served == pytest.approx(expected["pred_model"].tolist(), abs=REAL_TOLERANCE)
