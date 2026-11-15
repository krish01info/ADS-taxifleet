"""Phase 7: drift check of the frozen forecast on January 2025 (a runnable script, no model fitting).

Where it fits: the last step after the Phase 3 forecast was frozen. It reads the Phase 1 release and
data/forecasts/forecast_zone_hour.parquet fresh on every run, applies the rules written at the top of
src/drift_monitor.py, and writes tables, figures and a plain-English summary to data/drift/.
No random numbers are used anywhere, so there is no seed to set. January 2025 is used only to run this check. Run from anywhere inside the repo:
    python notebooks/07_drift_check.py
"""

import json
import shutil
import sys
from pathlib import Path

import duckdb
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import drift_monitor as monitor
from src.demand_features import LAG_COLUMNS, add_lag_features, make_civil_hour_table

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_INDEX_PATH = REPO_ROOT / "data/releases/phase1-2024-v1/metadata/table_index.json"
FORECAST_PATH = REPO_ROOT / "data/forecasts/forecast_zone_hour.parquet"
OUTPUT_FOLDER = REPO_ROOT / "data/drift"
REPORT_ASSETS_FOLDER = REPO_ROOT / "docs/report_assets"

METHOD_COLUMNS = ["pred_model", "pred_last_week", "pred_four_week_mean", "pred_frozen_profile",
                  "pred_frozen_profile_7d"]
METHOD_LABELS = {
    "pred_model": "Frozen model", "pred_last_week": "Last week",
    "pred_four_week_mean": "Mean of previous 4 weeks", "pred_frozen_profile": "Frozen 28-day profile",
    "pred_frozen_profile_7d": "Frozen 7-day profile",
}
HEADLINE_METHOD = "pred_model"
RECENT_NAME = "recent_nov_dec_2024"
DRIFT_NAME = "drift_jan_2025"
FIRST_CHECKED_DAY = "2024-12-30"
LAST_DRIFT_DAY = "2025-01-31"
JAN_BEFORE_LAST_DAY = "2025-01-04"
JAN_AFTER_FIRST_DAY = "2025-01-05"
JAN_2024_FIRST_DAY = "2024-01-01"
JAN_2024_LAST_DAY = "2024-01-31"
OCTOBER_FIRST_DAY = "2024-10-01"
TEXT_COLOUR = "#2b2b2b"
MODEL_COLOUR = "#0072B2"
BASELINE_COLOUR = "#D55E00"
GREY_COLOUR = "#8a8a8a"
LIGHT_GREY_COLOUR = "#c9c9c9"
FIGURE_DPI = 150
FIGURE_NAMES = ["fig_drift_weekly_wape.png", "fig_drift_psi_by_feature.png",
                "fig_drift_congestion_index.png", "fig_drift_hour_profile.png"]


def read_parquet_table(connection, path_or_folder, columns):
    """Read some columns of a parquet file, or of every parquet file under a folder, into pandas."""
    path = Path(path_or_folder)
    pattern = path.as_posix() if path.suffix == ".parquet" else (path / "**" / "*.parquet").as_posix()
    return connection.execute(f"select {columns} from read_parquet('{pattern}', hive_partitioning=true)").df()


def load_inputs():
    """Read the zone-hour table, the zones table and the forecast file. Returns three DataFrames."""
    table_index = json.loads(RELEASE_INDEX_PATH.read_text(encoding="utf-8"))
    connection = duckdb.connect()
    connection.execute("set TimeZone='UTC'")
    zone_hour = read_parquet_table(
        connection, REPO_ROOT / table_index["zone_hour"]["path"],
        "zone_id, event_date_nyc, hour_nyc, period_label, request_count, is_federal_holiday")
    zones = read_parquet_table(
        connection, REPO_ROOT / table_index["zones"]["path"],
        "zone_id, zone_name, borough, centroid_latitude, centroid_longitude, is_airport")
    forecast = read_parquet_table(
        connection, FORECAST_PATH,
        "zone_id, event_date_nyc, hour_nyc, period_label, request_count, is_scored_row, "
        + ", ".join(METHOD_COLUMNS))
    forecast["event_date_nyc"] = pd.to_datetime(forecast["event_date_nyc"]).dt.normalize()
    print(f"Read zone-hour rows: {len(zone_hour):,}; zones: {len(zones)}; forecast rows: {len(forecast):,}")
    if len(zones) != 263 or not zones["zone_id"].is_unique:
        raise ValueError("Expected 263 unique zones.")
    return zone_hour, zones, forecast


def split_periods(civil_table):
    """Return the reference (Jan-Oct 2024), recent (Nov-Dec 2024) and drift (Jan 2025) rows."""
    reference = civil_table[civil_table["period_label"].isin(monitor.REFERENCE_LABELS)]
    recent = civil_table[civil_table["period_label"] == monitor.RECENT_LABEL]
    drift = civil_table[civil_table["period_label"] == monitor.DRIFT_LABEL]
    for name, rows in [("reference", reference), ("recent", recent), ("drift", drift)]:
        print(f"{name}: {len(rows):,} civil zone-hour rows, "
              f"{int(rows['request_count'].isna().sum()):,} with a missing count")
    if reference.empty or recent.empty or drift.empty:
        raise ValueError("A period is empty.")
    return reference, recent, drift


def compare_one_feature(feature, reference, comparisons):
    """Compare one numeric column of each comparison period with the reference. Returns table rows."""
    reference_known = reference[reference[feature].notna()]
    edges = monitor.fit_reference_edges(reference_known, feature)
    reference_values = reference_known[feature].to_numpy(dtype=float)
    records = []
    for period_name, rows in comparisons.items():
        values = rows.loc[rows[feature].notna(), feature].to_numpy(dtype=float)
        records.append({"feature": feature, "comparison": period_name,
                        **monitor.compare_to_reference(reference_values, values, edges)})
    return records


def run_data_drift(reference, recent, drift):
    """Part (a): PSI and KS for request counts, the hour-of-day profile and the four lag features."""
    comparisons = {RECENT_NAME: recent, DRIFT_NAME: drift}
    records = []
    for feature in ["request_count", *LAG_COLUMNS]:
        records.extend(compare_one_feature(feature, reference, comparisons))
    for period_name, rows in comparisons.items():
        records.append({"feature": "hour_of_day_profile", "comparison": period_name,
                        **monitor.compare_hour_profiles(reference[reference["request_count"].notna()],
                                                        rows[rows["request_count"].notna()])})
    psi_table = pd.DataFrame(records)
    psi_table["flag"] = psi_table["psi_level"] == "alert"
    return psi_table


def run_january_season_comparison(civil_table):
    """Descriptive only: January 2025 against January 2024 (same season) for counts and hour profile."""
    dates = pd.to_datetime(civil_table["event_date_nyc"])
    january_2024 = civil_table[(dates >= JAN_2024_FIRST_DAY) & (dates <= JAN_2024_LAST_DAY)
                               & civil_table["request_count"].notna()]
    january_2025 = civil_table[(civil_table["period_label"] == monitor.DRIFT_LABEL)
                               & civil_table["request_count"].notna()]
    edges = monitor.fit_reference_edges(january_2024, "request_count")
    counts = monitor.compare_to_reference(january_2024["request_count"].to_numpy(dtype=float),
                                          january_2025["request_count"].to_numpy(dtype=float), edges)
    hours = monitor.compare_hour_profiles(january_2024, january_2025)
    return {"request_count": counts, "hour_of_day_profile": hours}


def make_weekly_count_psi(civil_table, reference):
    """PSI of the zone-hour request counts of each Monday-Sunday week against the reference."""
    reference_known = reference[reference["request_count"].notna()]
    edges = monitor.fit_reference_edges(reference_known, "request_count")
    reference_shares = monitor.count_shares(reference_known["request_count"].to_numpy(dtype=float), edges)
    later = civil_table[(civil_table["event_date_nyc"] >= pd.Timestamp(OCTOBER_FIRST_DAY))
                        & civil_table["request_count"].notna()]
    weekly_rows = monitor.add_week_start(later)
    records = []
    for week_start, rows in weekly_rows.groupby("week_start"):
        shares = monitor.count_shares(rows["request_count"].to_numpy(dtype=float), edges)
        records.append({"week_start": week_start,
                        "psi_request_counts": monitor.psi_from_shares(reference_shares, shares)})
    return pd.DataFrame(records)


def prepare_scored_rows(forecast):
    """Keep only scored rows and check that the actual and every prediction are present."""
    scored = forecast[forecast["is_scored_row"]].copy()
    scored["request_count"] = scored["request_count"].to_numpy(dtype=float, na_value=np.nan)
    needed = ["request_count", *METHOD_COLUMNS]
    if scored[needed].isna().any().any():
        raise ValueError("Scored rows have missing values.")
    print(f"Scored forecast rows: {len(scored):,} of {len(forecast):,}")
    print(scored.groupby("period_label").size().to_string())
    return scored


def run_error_drift(scored, weekly_count_psi):
    """Part (b): weekly errors, the October level, the over-limit weeks and the alert weeks."""
    validation_rows = scored[scored["period_label"] == monitor.VALIDATION_LABEL]
    levels = monitor.find_validation_level(validation_rows, METHOD_COLUMNS)
    weekly = monitor.score_weekly(scored, METHOD_COLUMNS)
    sorted_rows = monitor.add_week_start(scored.sort_values("event_date_nyc"))
    week_period = sorted_rows.groupby("week_start")["period_label"].last().rename("week_period")
    weekly = weekly.merge(week_period, on="week_start", how="left", validate="many_to_one")
    weekly = weekly.merge(weekly_count_psi, on="week_start", how="left", validate="many_to_one")
    weekly["validation_wape_percent"] = weekly["method"].map({m: v["WAPE_percent"] for m, v in levels.items()})
    weekly["limit_wape_percent"] = weekly["validation_wape_percent"] * (1 + monitor.WAPE_RELATIVE_LIMIT)
    weekly["is_over_limit"] = False
    weekly["is_alert_week"] = False
    for method in METHOD_COLUMNS:
        is_checked = ((weekly["method"] == method) & (weekly["days"] >= monitor.MIN_DAYS_PER_WEEK)
                      & (weekly["week_period"] != monitor.VALIDATION_LABEL))
        checked = weekly[is_checked].sort_values("week_start")
        over = monitor.mark_weeks_over_limit(checked["WAPE_percent"], levels[method]["WAPE_percent"])
        alerts = monitor.find_alert_weeks(over)
        weekly.loc[checked.index, "is_over_limit"] = over
        weekly.loc[checked.index, "is_alert_week"] = alerts
    return levels, weekly.sort_values(["method", "week_start"]).reset_index(drop=True)


def run_congestion_check(civil_table, scored, zones):
    """Part (c): descriptive demand index and errors, congestion-zone zones against all other zones."""
    congestion_ids = monitor.list_congestion_zone_ids(zones)
    borderline = monitor.list_borderline_zones(zones)
    print(f"Congestion-zone rule: {len(congestion_ids)} of {len(zones)} zones "
          f"({monitor.CONGESTION_BOROUGH}, centroid latitude < {monitor.CONGESTION_MAX_LATITUDE})")
    daily = monitor.sum_daily_requests(civil_table, congestion_ids)
    days = pd.date_range(FIRST_CHECKED_DAY, LAST_DRIFT_DAY)
    daily_baselines = monitor.make_daily_baselines(daily, days)
    windows = {
        "jan_1_4": (monitor.DRIFT_START_DATE, JAN_BEFORE_LAST_DAY),
        "jan_5_31": (JAN_AFTER_FIRST_DAY, LAST_DRIFT_DAY),
        "jan_1_31": (monitor.DRIFT_START_DATE, LAST_DRIFT_DAY),
    }
    window_index = {name: monitor.make_window_index(daily_baselines, first, last)
                    for name, (first, last) in windows.items()}
    weekly_index = []
    for week_start in pd.date_range(FIRST_CHECKED_DAY, LAST_DRIFT_DAY, freq=f"{monitor.DAYS_PER_WEEK}D"):
        week_end = min(week_start + pd.Timedelta(days=monitor.DAYS_PER_WEEK - 1), pd.Timestamp(LAST_DRIFT_DAY))
        weekly_index.append({"week_start": week_start,
                             **monitor.make_window_index(daily_baselines, week_start, week_end)})
    scored_groups = scored.copy()
    scored_groups["zone_group"] = np.where(scored_groups["zone_id"].isin(congestion_ids), "congestion", "rest")
    scored_groups["window"] = assign_error_windows(scored_groups["event_date_nyc"])
    error_by_group = monitor.score_groups(scored_groups, ["window", "zone_group"], [HEADLINE_METHOD,
                                                                                    "pred_four_week_mean"])
    return {"congestion_ids": congestion_ids, "borderline": borderline, "daily_baselines": daily_baselines,
            "window_index": window_index, "weekly_index": pd.DataFrame(weekly_index),
            "error_by_group": error_by_group}


def assign_error_windows(dates):
    """Name the comparison window of each date: October, Nov-Dec, 1-4 January, 5-31 January."""
    conditions = [
        dates < pd.Timestamp("2024-11-01"),
        dates < pd.Timestamp(monitor.DRIFT_START_DATE),
        dates <= pd.Timestamp(JAN_BEFORE_LAST_DAY),
    ]
    return np.select(conditions, ["oct_2024", "nov_dec_2024", "jan_1_4"], default="jan_5_31")


def pick_strongest_feature(psi_table, comparison):
    """Return the feature with the largest PSI for one comparison period."""
    rows = psi_table[psi_table["comparison"] == comparison]
    best = rows.loc[rows["psi"].idxmax()]
    return {"feature": best["feature"], "psi": float(best["psi"])}


def summarise_error_monitor(levels, weekly):
    """Return the headline numbers of the error monitor for the frozen model."""
    model_weeks = weekly[(weekly["method"] == HEADLINE_METHOD) & (weekly["days"] >= monitor.MIN_DAYS_PER_WEEK)]
    summary = {"validation_wape_percent": levels[HEADLINE_METHOD]["WAPE_percent"],
               "limit_wape_percent": levels[HEADLINE_METHOD]["WAPE_percent"] * (1 + monitor.WAPE_RELATIVE_LIMIT)}
    for label, period in [("january", monitor.DRIFT_LABEL), ("recent", monitor.RECENT_LABEL)]:
        period_weeks = model_weeks[model_weeks["week_period"] == period]
        first_week, alert_count = monitor.describe_alert_weeks(period_weeks, period_weeks["is_alert_week"])
        summary[f"{label}_alert_first_week"] = first_week
        summary[f"{label}_alert_run_started_week"] = (
            None if first_week is None else (pd.Timestamp(first_week) - pd.Timedelta(
                days=monitor.DAYS_PER_WEEK * (monitor.CONSECUTIVE_WEEKS_FOR_ALERT - 1))).date().isoformat())
        summary[f"{label}_alert_weeks"] = alert_count
        summary[f"{label}_weeks_over_limit"] = int(period_weeks["is_over_limit"].sum())
        summary[f"{label}_max_weekly_wape_percent"] = float(period_weeks["WAPE_percent"].max())
    after_october = model_weeks[model_weeks["week_period"] != monitor.VALIDATION_LABEL]
    summary["weeks_over_limit_after_october"] = after_october.loc[after_october["is_over_limit"], "week_start"]         .dt.strftime("%Y-%m-%d").tolist()
    return summary


def summarise_baseline_alerts(weekly):
    """Return, for every method, its first alert week after October and how many weeks were over its limit."""
    result = {}
    for method in METHOD_COLUMNS:
        method_weeks = weekly[weekly["method"] == method]
        first_week, alert_count = monitor.describe_alert_weeks(method_weeks, method_weeks["is_alert_week"])
        result[method] = {"first_alert_week": first_week, "alert_weeks": alert_count,
                          "weeks_over_limit": int(method_weeks["is_over_limit"].sum())}
    return result


def to_records(table):
    """Turn a DataFrame into plain Python records for JSON (dates as text, NaN as null)."""
    result = table.copy()
    for column in result.columns:
        if pd.api.types.is_datetime64_any_dtype(result[column]):
            result[column] = result[column].dt.strftime("%Y-%m-%d")
    return json.loads(result.to_json(orient="records"))


def style_axes(axis):
    """Light, thin axes in one place so every figure looks the same."""
    for side in ("top", "right"):
        axis.spines[side].set_visible(False)
    axis.grid(axis="y", color="#e6e6e6", linewidth=0.6)
    axis.set_axisbelow(True)
    axis.tick_params(colors=TEXT_COLOUR)


def plot_weekly_wape(weekly, levels, path):
    """Figure 1: weekly WAPE of the frozen model and baselines, with the alert limit."""
    fig, axis = plt.subplots(figsize=(9, 4.2))
    style_axes(axis)
    drift_start = pd.Timestamp(monitor.DRIFT_START_DATE)
    pricing_start = pd.Timestamp(monitor.CONGESTION_PRICING_START_DATE)
    for method in METHOD_COLUMNS:
        rows = weekly[(weekly["method"] == method) & (weekly["days"] >= monitor.MIN_DAYS_PER_WEEK)]
        is_headline = method == HEADLINE_METHOD
        colour = MODEL_COLOUR if is_headline else (BASELINE_COLOUR if method == "pred_four_week_mean" else LIGHT_GREY_COLOUR)
        axis.plot(rows["week_start"], rows["WAPE_percent"], color=colour, linewidth=2.2 if is_headline else 1.3,
                  marker="o", markersize=4, zorder=3 if is_headline else 2, label=METHOD_LABELS[method])
    limit = levels[HEADLINE_METHOD]["WAPE_percent"] * (1 + monitor.WAPE_RELATIVE_LIMIT)
    axis.axhline(limit, color=TEXT_COLOUR, linestyle="--", linewidth=1)
    axis.annotate(f"Alert limit for the frozen model: {limit:.1f}% (October level + 25%)",
                  (weekly["week_start"].min(), limit), xytext=(0, 4), textcoords="offset points", fontsize=8)
    alert_rows = weekly[(weekly["method"] == HEADLINE_METHOD) & weekly["is_alert_week"]]
    axis.scatter(alert_rows["week_start"], alert_rows["WAPE_percent"], s=60, facecolors="none",
                 edgecolors=TEXT_COLOUR, linewidths=1.4, zorder=4, label="Alert week (2 weeks over limit)")
    axis.axvspan(drift_start, weekly["week_start"].max() + pd.Timedelta(days=monitor.DAYS_PER_WEEK),
                 color="#f1f1f1", zorder=0)
    axis.axvline(pricing_start, color=GREY_COLOUR, linewidth=1, linestyle=":")
    axis.annotate("5 Jan 2025: congestion pricing began", (pricing_start, axis.get_ylim()[1]), xytext=(4, -12),
                  textcoords="offset points", fontsize=8, ha="left", color=TEXT_COLOUR)
    axis.set(ylabel="Weekly WAPE (%)", title="Weekly forecast error against the October 2024 level",
             xlim=(weekly["week_start"].min() - pd.Timedelta(days=3), weekly["week_start"].max() + pd.Timedelta(days=10)))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    axis.legend(loc="upper left", fontsize=8, frameon=False)
    fig.text(0.01, 0.01, "Week = Monday start. Shaded: January 2025. Descriptive; no cause is claimed.",
             fontsize=7, color=GREY_COLOUR)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)


def plot_psi_bars(psi_table, path):
    """Figure 2: PSI of each feature, Nov-Dec 2024 and January 2025, with the 0.1 and 0.2 lines."""
    features = ["request_count", "hour_of_day_profile", *LAG_COLUMNS]
    labels = ["Hourly request count", "Hour-of-day profile", "Lag 7 days", "Lag 14 days", "Lag 21 days", "Lag 28 days"]
    positions = np.arange(len(features))
    bar_height = 0.38
    fig, axis = plt.subplots(figsize=(8, 4.2))
    style_axes(axis)
    axis.grid(axis="x", color="#e6e6e6", linewidth=0.6)
    axis.grid(axis="y", visible=False)
    for offset, name, colour, label in [(-bar_height / 2, RECENT_NAME, GREY_COLOUR, "Nov-Dec 2024"),
                                        (bar_height / 2, DRIFT_NAME, MODEL_COLOUR, "January 2025")]:
        values = [float(psi_table[(psi_table["feature"] == f) & (psi_table["comparison"] == name)]["psi"].iloc[0])
                  for f in features]
        axis.barh(positions + offset, values, height=bar_height, color=colour, label=label)
        for position, value in zip(positions, values, strict=True):
            axis.text(value + 0.004, position + offset, f"{value:.3f}", va="center", fontsize=7, color=TEXT_COLOUR)
    axis.axvline(monitor.PSI_WATCH_LEVEL, color=GREY_COLOUR, linestyle=":", linewidth=1)
    axis.axvline(monitor.PSI_ALERT_LEVEL, color=TEXT_COLOUR, linestyle="--", linewidth=1)
    axis.text(monitor.PSI_WATCH_LEVEL, -0.4, "watch 0.1", fontsize=8, ha="center", va="top")
    axis.text(monitor.PSI_ALERT_LEVEL, -0.4, "alert 0.2", fontsize=8, ha="right", va="top")
    axis.set(yticks=positions, yticklabels=labels, xlabel="Population Stability Index against Jan-Oct 2024",
             title="Data drift by feature")
    axis.invert_yaxis()
    axis.legend(loc="center right", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)


def plot_congestion_index(daily_baselines, path):
    """Figure 3: daily demand index (actual / baseline) for congestion-zone zones and all other zones."""
    january = daily_baselines.loc[pd.Timestamp(monitor.DRIFT_START_DATE):]
    pricing_start = pd.Timestamp(monitor.CONGESTION_PRICING_START_DATE)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
    for axis, baseline, title in [(axes[0], "last_year", "Baseline: same weekdays 364 days earlier (Jan 2024)"),
                                  (axes[1], "december", "Baseline: same weekdays, 2-22 Dec 2024")]:
        style_axes(axis)
        for group, colour, label in [("congestion", MODEL_COLOUR, "Congestion-zone zones"),
                                     ("rest", GREY_COLOUR, "All other zones")]:
            index = january[f"actual_{group}"] / january[f"{baseline}_{group}"]
            axis.plot(january.index, index, color=colour, linewidth=1.8, label=label)
        axis.axhline(1.0, color=LIGHT_GREY_COLOUR, linewidth=1)
        axis.axvline(pricing_start, color=TEXT_COLOUR, linestyle=":", linewidth=1)
        axis.set_title(title, fontsize=9)
        axis.xaxis.set_major_locator(mdates.DayLocator(interval=7))
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    axes[0].set_ylabel("Demand index (1.0 = same as baseline)")
    axes[0].legend(loc="lower left", fontsize=8, frameon=False)
    axes[0].annotate("5 Jan", (pricing_start, axes[0].get_ylim()[1]), xytext=(3, -10), textcoords="offset points",
                     fontsize=8)
    fig.suptitle("Daily requests in January 2025 relative to a baseline (descriptive only)", fontsize=10)
    fig.text(0.01, 0.01, "Holidays are not aligned with the baselines (1 Jan, 20 Jan 2025). No cause is claimed.",
             fontsize=7, color=GREY_COLOUR)
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)


def plot_hour_profiles(reference, recent, drift, path):
    """Figure 4: share of requests by New York clock hour, reference against Nov-Dec and January."""
    fig, axis = plt.subplots(figsize=(8, 3.8))
    style_axes(axis)
    hours = np.arange(monitor.HOURS_PER_DAY)
    for rows, colour, label in [(reference, GREY_COLOUR, "Jan-Oct 2024"), (recent, BASELINE_COLOUR, "Nov-Dec 2024"),
                                (drift, MODEL_COLOUR, "January 2025")]:
        shares = monitor.hour_profile_shares(rows[rows["request_count"].notna()])
        axis.plot(hours, 100 * shares, color=colour, linewidth=1.8, label=label)
    axis.set(xlabel="New York clock hour", ylabel="Share of daily requests (%)", xticks=range(0, 24, 3),
             title="Hour-of-day demand profile")
    axis.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)


def main():
    """Run the whole drift check and save everything to data/drift/ (and the figures to docs/report_assets/)."""
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    REPORT_ASSETS_FOLDER.mkdir(parents=True, exist_ok=True)
    zone_hour, zones, forecast = load_inputs()
    civil_table = make_civil_hour_table(zone_hour)
    civil_with_lags = add_lag_features(civil_table)
    reference, recent, drift = split_periods(civil_with_lags)

    psi_table = run_data_drift(reference, recent, drift)
    season_comparison = run_january_season_comparison(civil_with_lags)
    weekly_count_psi = make_weekly_count_psi(civil_with_lags, reference)
    scored = prepare_scored_rows(forecast)
    levels, weekly = run_error_drift(scored, weekly_count_psi)
    congestion = run_congestion_check(civil_table, scored, zones)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    shown_columns = ["feature", "comparison", "psi", "psi_level", "ks_statistic", "ks_p_value"]
    print("\nDATA DRIFT (PSI > 0.2 = alert, 0.1-0.2 = watch)")
    print(psi_table[shown_columns].round(4).to_string(index=False))
    print("\nWEEKLY ERROR, frozen model and baselines")
    print(weekly[weekly["method"] == HEADLINE_METHOD][["week_start", "days", "week_period", "MAE", "WAPE_percent", "bias_percent",
                                                       "limit_wape_percent", "is_over_limit", "is_alert_week"]].round(2).to_string(index=False))
    print("\nOctober (validation) level:", {m: round(v["WAPE_percent"], 2) for m, v in levels.items()})
    print("\nERRORS BY WINDOW AND ZONE GROUP (descriptive)")
    print(congestion["error_by_group"].round(2).to_string(index=False))
    print("\nDEMAND INDEX BY WINDOW (descriptive)")
    print(pd.DataFrame(congestion["window_index"]).T.round(4).to_string())
    print("\nCongestion-rule borderline zones:")
    print(congestion["borderline"][["zone_id", "zone_name", "centroid_latitude"]].round(4).to_string(index=False))

    error_summary = summarise_error_monitor(levels, weekly)
    january_psi = psi_table[psi_table["comparison"] == DRIFT_NAME]
    summary = {
        "rules": {"psi_watch": monitor.PSI_WATCH_LEVEL, "psi_alert": monitor.PSI_ALERT_LEVEL,
                  "wape_relative_limit": monitor.WAPE_RELATIVE_LIMIT,
                  "consecutive_weeks_for_alert": monitor.CONSECUTIVE_WEEKS_FOR_ALERT,
                  "min_days_per_week": monitor.MIN_DAYS_PER_WEEK,
                  "congestion_rule": f"borough {monitor.CONGESTION_BOROUGH}, centroid latitude < {monitor.CONGESTION_MAX_LATITUDE}"},
        "periods": {"reference": "Jan-Oct 2024 (train + validation)", "recent": "Nov-Dec 2024 (test)",
                    "drift": "January 2025 (used only for this check)"},
        "data_drift": to_records(psi_table),
        "data_drift_alert_features_january": january_psi.loc[january_psi["flag"], "feature"].tolist(),
        "data_drift_watch_features_january": january_psi.loc[january_psi["psi_level"] == "watch", "feature"].tolist(),
        "data_drift_strongest_feature_january": pick_strongest_feature(psi_table, DRIFT_NAME),
        "january_2025_vs_january_2024_descriptive": season_comparison,
        "validation_level_october_2024": levels,
        "error_monitor_frozen_model": error_summary,
        "error_monitor_by_method": summarise_baseline_alerts(weekly),
        "congestion_zone": {"zone_count": len(congestion["congestion_ids"]), "zone_ids": congestion["congestion_ids"],
                            "borderline_zones": to_records(congestion["borderline"][["zone_id", "zone_name", "centroid_latitude"]])},
        "demand_index_by_window": congestion["window_index"],
        "error_by_window_and_zone_group": to_records(congestion["error_by_group"]),
    }
    summary["plain_english"] = monitor.write_plain_summary(summary)

    psi_table.to_csv(OUTPUT_FOLDER / "psi_by_feature.csv", index=False)
    weekly.to_csv(OUTPUT_FOLDER / "weekly_error.csv", index=False)
    congestion["weekly_index"].to_csv(OUTPUT_FOLDER / "congestion_weekly_index.csv", index=False)
    congestion["daily_baselines"].to_csv(OUTPUT_FOLDER / "congestion_daily_baselines.csv")
    congestion["error_by_group"].to_csv(OUTPUT_FOLDER / "error_by_window_and_zone_group.csv", index=False)
    (OUTPUT_FOLDER / "drift_summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    (OUTPUT_FOLDER / "plain_english_summary.txt").write_text(summary["plain_english"] + "\n", encoding="utf-8")

    figure_paths = {name: OUTPUT_FOLDER / name for name in FIGURE_NAMES}
    plot_weekly_wape(weekly, levels, figure_paths["fig_drift_weekly_wape.png"])
    plot_psi_bars(psi_table, figure_paths["fig_drift_psi_by_feature.png"])
    plot_congestion_index(congestion["daily_baselines"], figure_paths["fig_drift_congestion_index.png"])
    plot_hour_profiles(reference, recent, drift, figure_paths["fig_drift_hour_profile.png"])
    for name, path in figure_paths.items():
        shutil.copy(path, REPORT_ASSETS_FOLDER / name)
    print("\nPLAIN-ENGLISH SUMMARY")
    print(summary["plain_english"])
    print(f"\nSaved tables, drift_summary.json and {len(FIGURE_NAMES)} figures to {OUTPUT_FOLDER.relative_to(REPO_ROOT)}"
          f" (figures also copied to {REPORT_ASSETS_FOLDER.relative_to(REPO_ROOT)})")


if __name__ == "__main__":
    main()
