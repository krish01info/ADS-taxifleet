"""Builds the day-ahead demand forecast features (Phase 3, notebook 03b).

The forecast for a zone and hour may only use information known at least 7 days
earlier. So the demand inputs are the counts from the same zone, the same civil
weekday and the same clock hour 7, 14, 21 and 28 days before the target hour.
Calendar and zone geography are known in advance too. Nothing else about demand
(no recent hours, no fares, no weather) is ever used.

Order of use in the notebook: make_civil_hour_table -> build_features ->
build_frozen_profiles / add_baseline_columns -> score_forecast.
"""

from pathlib import Path

import numpy as np
import pandas as pd

LAG_WEEKS = (1, 2, 3, 4)
DAYS_PER_WEEK = 7
MIN_LAGS_FOR_MEAN = 2
HOURS_PER_DAY = 24
FROZEN_PROFILE_DAYS = {"7d": 7, "28d": 28}
CIVIL_KEY = ["zone_id", "event_date_nyc", "hour_nyc"]
LAG_COLUMNS = [f"lag_{DAYS_PER_WEEK * weeks}d" for weeks in LAG_WEEKS]
CALENDAR_COLUMNS = [
    "hour", "weekday", "month", "is_weekend", "is_federal_holiday",
    "hour_sin", "hour_cos", "weekday_sin", "weekday_cos",
]
GEOGRAPHY_COLUMNS = ["centroid_latitude", "centroid_longitude", "is_airport"]
LAG_HOLIDAY_COLUMNS = [f"{name}_is_holiday" for name in LAG_COLUMNS]
LAG_SUMMARY_COLUMNS = ["lag_mean_4w", "lag_7d_ratio"]


def find_repo_root(start=None):
    """Walk up from the given folder to the folder that holds repo_instructions.md."""
    start = Path.cwd() if start is None else Path(start)
    for folder in [start, *start.resolve().parents]:
        if (folder / "repo_instructions.md").is_file():
            return folder
    raise FileNotFoundError("Run from inside the repository: repo_instructions.md not found.")


def make_civil_hour_table(zone_hour_rows):
    """Return one row per zone, New York date and clock hour.

    The repeated clock hour on the autumn clock change appears twice with the same
    civil key. We average its two counts, but if either count is null the result
    stays null (a missing count is never treated as zero).
    """
    needed = CIVIL_KEY + ["request_count", "is_federal_holiday", "period_label"]
    missing = [name for name in needed if name not in zone_hour_rows.columns]
    if missing:
        raise ValueError(f"zone-hour rows are missing columns: {missing}")
    rows = zone_hour_rows[needed].copy()
    rows["event_date_nyc"] = pd.to_datetime(rows["event_date_nyc"]).dt.normalize()
    rows["request_count"] = rows["request_count"].to_numpy(dtype=float, na_value=np.nan)
    rows["is_null_count"] = rows["request_count"].isna().astype(int)
    rows["is_federal_holiday"] = rows["is_federal_holiday"].astype(int)
    civil = rows.groupby(CIVIL_KEY, as_index=False).agg(
        request_count=("request_count", "mean"),
        null_rows=("is_null_count", "sum"),
        is_federal_holiday=("is_federal_holiday", "max"),
        period_label=("period_label", "first"),
    )
    civil.loc[civil["null_rows"] > 0, "request_count"] = np.nan
    civil = civil.drop(columns="null_rows")
    civil["is_federal_holiday"] = civil["is_federal_holiday"].astype(bool)
    if civil.duplicated(CIVIL_KEY).any():
        raise ValueError("Civil key is still duplicated after averaging.")
    print(f"Civil hour table: {len(rows):,} zone-hour rows -> {len(civil):,} civil rows")
    return civil


def add_lag_features(civil_table):
    """Add lag_7d, lag_14d, lag_21d, lag_28d and their summaries.

    A lag is the count at the same zone and clock hour exactly 7*k civil days
    earlier. Each lag is a plain look-up of an older row, so no value from the
    last 7 days, or from the target hour onwards, can enter. We also note whether
    each lag day was a federal holiday: that is calendar knowledge, not demand.
    """
    result = civil_table.copy()
    source = civil_table[CIVIL_KEY + ["request_count", "is_federal_holiday"]]
    for weeks, lag_name, holiday_name in zip(LAG_WEEKS, LAG_COLUMNS, LAG_HOLIDAY_COLUMNS, strict=True):
        older = source.copy()
        older["event_date_nyc"] = older["event_date_nyc"] + pd.Timedelta(days=DAYS_PER_WEEK * weeks)
        older = older.rename(columns={"request_count": lag_name, "is_federal_holiday": holiday_name})
        before = len(result)
        result = result.merge(older, on=CIVIL_KEY, how="left", validate="one_to_one")
        if len(result) != before:
            raise ValueError(f"Row count changed while adding {lag_name}.")
    result[LAG_HOLIDAY_COLUMNS] = result[LAG_HOLIDAY_COLUMNS].fillna(False).astype(float)
    lags = result[LAG_COLUMNS]
    result["lag_mean_4w"] = lags.mean(axis=1).where(lags.notna().sum(axis=1) >= MIN_LAGS_FOR_MEAN)
    result["lag_7d_ratio"] = (result["lag_7d"] + 1) / (result["lag_mean_4w"] + 1)
    print(f"Lag features added: {len(result):,} rows")
    return result


def add_calendar_features(table):
    """Add hour, weekday, month, weekend flag and sine/cosine of hour and weekday."""
    result = table.copy()
    result["hour"] = result["hour_nyc"].astype(float)
    result["weekday"] = result["event_date_nyc"].dt.dayofweek.astype(float)
    result["month"] = result["event_date_nyc"].dt.month.astype(float)
    result["is_weekend"] = (result["weekday"] >= 5).astype(float)
    result["is_federal_holiday"] = result["is_federal_holiday"].astype(float)
    result["hour_sin"] = np.sin(2 * np.pi * result["hour"] / HOURS_PER_DAY)
    result["hour_cos"] = np.cos(2 * np.pi * result["hour"] / HOURS_PER_DAY)
    result["weekday_sin"] = np.sin(2 * np.pi * result["weekday"] / DAYS_PER_WEEK)
    result["weekday_cos"] = np.cos(2 * np.pi * result["weekday"] / DAYS_PER_WEEK)
    return result


def list_borough_columns(boroughs):
    """Return the one-hot column names for the given borough names."""
    return [f"borough_{name}" for name in boroughs]


def add_geography_features(table, zones, boroughs):
    """Add centroid latitude/longitude, airport flag and borough one-hot columns."""
    zone_columns = ["zone_id", "borough", *GEOGRAPHY_COLUMNS]
    zone_info = zones[zone_columns].copy()
    if zone_info["zone_id"].duplicated().any():
        raise ValueError("The zones table has a repeated zone_id.")
    zone_info["is_airport"] = zone_info["is_airport"].astype(float)
    before = len(table)
    result = table.merge(zone_info, on="zone_id", how="left", validate="many_to_one")
    if len(result) != before:
        raise ValueError("Row count changed while adding geography.")
    if result["borough"].isna().any():
        raise ValueError("Some zone_ids are not in the zones table.")
    unknown = set(result["borough"]) - set(boroughs)
    if unknown:
        raise ValueError(f"Unexpected boroughs: {sorted(unknown)}")
    for name, column in zip(boroughs, list_borough_columns(boroughs), strict=True):
        result[column] = (result["borough"] == name).astype(float)
    print(f"Geography added: {len(result):,} rows")
    return result


def list_feature_columns(boroughs):
    """Return the model input columns in a fixed order (demand enters only through the lags)."""
    return (CALENDAR_COLUMNS + GEOGRAPHY_COLUMNS + list_borough_columns(boroughs)
            + LAG_COLUMNS + LAG_HOLIDAY_COLUMNS + LAG_SUMMARY_COLUMNS)


def build_features(civil_table, zones, boroughs):
    """Build every model input for every civil row of the table."""
    with_lags = add_lag_features(civil_table)
    with_calendar = add_calendar_features(with_lags)
    return add_geography_features(with_calendar, zones, boroughs)


def build_frozen_profiles(civil_table, end_date):
    """Build the fixed profiles Krish defined: mean by zone, weekday and clock hour.

    The 7-day profile averages the last 7 days before end_date (one value needed).
    The 28-day profile averages the last 28 days (all four values needed). Rows
    from end_date onwards are never used. Missing history stays missing.
    """
    end_date = pd.Timestamp(end_date)
    rows = civil_table.loc[civil_table["event_date_nyc"] < end_date,
                           CIVIL_KEY + ["request_count"]].copy()
    if rows.empty:
        raise ValueError("No training rows before the profile end date.")
    rows["weekday"] = rows["event_date_nyc"].dt.dayofweek
    profile_keys = ["zone_id", "weekday", "hour_nyc"]
    profiles = None
    for label, days in FROZEN_PROFILE_DAYS.items():
        recent = rows.loc[rows["event_date_nyc"] >= end_date - pd.Timedelta(days=days)]
        grouped = recent.groupby(profile_keys)["request_count"].agg(["mean", "count"])
        values_needed = days // DAYS_PER_WEEK
        column = grouped["mean"].where(grouped["count"] == values_needed).rename(f"frozen_profile_{label}")
        profiles = column.to_frame() if profiles is None else profiles.join(column, how="outer")
    return profiles.reset_index()


def add_baseline_columns(feature_table, profiles):
    """Add the three baseline predictions next to the model inputs.

    pred_last_week is lag_7d. pred_four_week_mean needs all four lags.
    The frozen profiles come from the fixed training profile table.
    """
    result = feature_table.copy()
    result["pred_last_week"] = result["lag_7d"]
    all_four = result[LAG_COLUMNS].notna().all(axis=1)
    result["pred_four_week_mean"] = result[LAG_COLUMNS].mean(axis=1).where(all_four)
    result["weekday_int"] = result["event_date_nyc"].dt.dayofweek
    before = len(result)
    result = result.merge(
        profiles.rename(columns={"weekday": "weekday_int"}),
        on=["zone_id", "weekday_int", "hour_nyc"], how="left", validate="many_to_one")
    if len(result) != before:
        raise ValueError("Row count changed while adding frozen profiles.")
    result = result.drop(columns="weekday_int")
    result["pred_frozen_profile"] = result["frozen_profile_28d"]
    result["pred_frozen_profile_7d"] = result["frozen_profile_7d"]
    print(f"Baselines added: {len(result):,} rows")
    return result


def mark_common_rows(table, prediction_columns):
    """Flag rows where the actual count and every listed prediction are present."""
    needed = ["request_count", *prediction_columns]
    return table[needed].notna().all(axis=1)


def score_forecast(actual, predicted):
    """Return MAE, RMSE and WAPE (percent) of predicted against actual counts."""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if actual.shape != predicted.shape or actual.size == 0:
        raise ValueError("Need two non-empty vectors of the same length.")
    if np.isnan(actual).any() or np.isnan(predicted).any():
        raise ValueError("Scoring needs no missing values; filter to common rows first.")
    error = predicted - actual
    wape = 100 * np.abs(error).sum() / actual.sum() if actual.sum() > 0 else np.nan
    return {
        "rows": int(actual.size),
        "MAE": float(np.abs(error).mean()),
        "RMSE": float(np.sqrt((error ** 2).mean())),
        "WAPE_percent": float(wape),
    }


def score_by_group(table, group_column, method_columns):
    """Score every method inside every value of group_column (rows must be common)."""
    records = []
    for group_value, group_rows in table.groupby(group_column, observed=True):
        for method in method_columns:
            scores = score_forecast(group_rows["request_count"], group_rows[method])
            records.append({group_column: group_value, "method": method, **scores})
    return pd.DataFrame(records)


def fit_count_scale(actual, predicted):
    """Return sum(actual) / sum(predicted): one global factor that fixes the total."""
    predicted_total = float(np.sum(predicted))
    if predicted_total <= 0:
        raise ValueError("Cannot scale predictions that sum to zero.")
    return float(np.sum(actual)) / predicted_total
