"""Phase 7 drift monitor: simple, pre-declared rules for "has the data or the forecast changed?".

Where it fits: Phase 3 froze a demand forecast (data/forecasts/frozen_choice.json). This file holds
the small functions that the drift check (notebooks/07_drift_check.py) uses to ask whether the
data coming in, or the errors of the frozen forecast, look different from what the forecast was
built on. Nothing here fits or tunes a model.

MONITORING RULES (written down BEFORE any January 2025 result was computed)
    Periods
        reference  Jan-Oct 2024 (period_label train + validation). Defines what "normal" is.
        recent     Nov-Dec 2024 (period_label test). Already examined by the team earlier.
        drift      January 2025 (period_label drift). Used ONLY to run this check, never for
                   fitting, tuning or choosing anything.
    (a) Data drift. For each feature below we compare a period with the reference.
        Population Stability Index (PSI): cut the reference into 10 bins of equal share
        (deciles of the reference), count the share of the comparison data in each bin, and add up
        (comparison share - reference share) x ln(comparison share / reference share).
        PSI < 0.1 = ok, 0.1 to 0.2 = watch, PSI > 0.2 = ALERT (flag).
        We also report the two-sample Kolmogorov-Smirnov (KS) statistic D (largest gap between the
        two cumulative curves). With about 100,000+ rows any tiny change gives a tiny p-value, so
        the flag uses PSI only; KS is shown as a second view of the size of the shift.
        Features: (i) hourly request count per zone-hour, (ii) hour-of-day demand profile (share
        of all requests that fall in each New York clock hour; PSI over the 24 hours, KS as the
        largest gap between the two cumulative hour-of-day curves), (iii) the model's lag
        features lag_7d, lag_14d, lag_21d and lag_28d (same zone, same clock hour 7/14/21/28
        days earlier).
        The data-drift monitor FIRES when at least one feature has PSI > 0.2.
    (b) Error drift. For every ISO-style week (Monday to Sunday, New York dates) with at least
        5 scored days, compute the weekly WAPE (total absolute error / total actual requests)
        and MAE of the frozen model and of the baselines on scored rows (rows where the actual
        and every prediction exist). The yardstick is the WAPE of the same method over October
        2024 (the validation level). A week is "over the limit" when its WAPE is more than 25%
        above that level (WAPE > 1.25 x October WAPE). The error monitor FIRES at the second of
        two consecutive weeks over the limit. The headline monitor is the frozen model
        (pred_model); the baselines are shown for context with the same rule.
    (c) Descriptive only, never an alert. Compare 1-4 January with 5-31 January 2025, and zones
        inside the congestion zone with all other zones. Congestion pricing (a fee for entering
        Manhattan south of 60th Street) began on 5 January 2025. Our zone rule: borough
        Manhattan and zone centroid latitude below 40.7685 N. This is an approximation made from
        centroids: a few zones that straddle 60th Street (for example Lenox Hill, Roosevelt
        Island) are on one side or the other by their centroid only. Demand index = requests in a
        period divided by requests in a matching baseline period (the same weekdays 364 days
        earlier, i.e. January 2024, and the same weekdays in 2-22 December 2024).
        These comparisons describe what changed; they say nothing about why.
    No causal claim. A fired alert means "the data or errors look different"; January 2025 also
    differs in weather, holidays and season, so no cause (including the pricing policy) is
    assumed or tested here.

Design promise: the only function that learns anything from data is fit_reference_edges (the PSI
bin edges), and it refuses any table that contains January 2025 or the 'drift' label.
"""

import numpy as np
import pandas as pd
from scipy import stats

from src.demand_features import score_forecast

REFERENCE_LABELS = ("train", "validation")
RECENT_LABEL = "test"
DRIFT_LABEL = "drift"
VALIDATION_LABEL = "validation"
DRIFT_START_DATE = "2025-01-01"
CONGESTION_PRICING_START_DATE = "2025-01-05"

PSI_BIN_COUNT = 10
PSI_WATCH_LEVEL = 0.1
PSI_ALERT_LEVEL = 0.2
PSI_MIN_SHARE = 0.0001
HOURS_PER_DAY = 24

WAPE_RELATIVE_LIMIT = 0.25
CONSECUTIVE_WEEKS_FOR_ALERT = 2
MIN_DAYS_PER_WEEK = 5
DAYS_PER_WEEK = 7

CONGESTION_BOROUGH = "Manhattan"
CONGESTION_MAX_LATITUDE = 40.7685
BORDERLINE_LATITUDE_MARGIN = 0.01
SAME_WEEKDAY_LAST_YEAR_DAYS = 364
DECEMBER_BASELINE_FIRST_DAY = "2024-12-02"
DECEMBER_BASELINE_LAST_DAY = "2024-12-22"


def check_no_drift_rows(table, purpose):
    """Raise an error if the table holds January 2025 or any row labelled 'drift'.

    Called before anything is learned from data, so the drift month can never leak into a fit.
    """
    if (table["period_label"] == DRIFT_LABEL).any():
        raise ValueError(f"{purpose}: the table contains rows labelled '{DRIFT_LABEL}'.")
    if (pd.to_datetime(table["event_date_nyc"]) >= pd.Timestamp(DRIFT_START_DATE)).any():
        raise ValueError(f"{purpose}: the table contains dates from {DRIFT_START_DATE} on.")


def make_bin_edges(reference_values, bin_count=PSI_BIN_COUNT):
    """Return the inner bin edges: the deciles of the reference values (repeated edges removed)."""
    reference_values = np.asarray(reference_values, dtype=float)
    if reference_values.size == 0 or np.isnan(reference_values).any():
        raise ValueError("Reference values must be non-empty and have no missing values.")
    shares = np.arange(1, bin_count) / bin_count
    return np.unique(np.quantile(reference_values, shares))


def fit_reference_edges(reference_table, column):
    """Make the PSI bin edges for one column of the reference table (never January 2025)."""
    check_no_drift_rows(reference_table, "fit_reference_edges")
    return make_bin_edges(reference_table[column].to_numpy(dtype=float))


def count_shares(values, edges):
    """Return the share of the values that falls in each bin (bins are (edge_i-1, edge_i])."""
    values = np.asarray(values, dtype=float)
    if values.size == 0 or np.isnan(values).any():
        raise ValueError("Values must be non-empty and have no missing values.")
    bin_numbers = np.searchsorted(edges, values, side="left")
    counts = np.bincount(bin_numbers, minlength=len(edges) + 1)
    return counts / counts.sum()


def psi_from_shares(reference_shares, comparison_shares):
    """Return the PSI of two lists of shares. Empty bins are floored at a tiny share."""
    reference_shares = np.clip(np.asarray(reference_shares, dtype=float), PSI_MIN_SHARE, None)
    comparison_shares = np.clip(np.asarray(comparison_shares, dtype=float), PSI_MIN_SHARE, None)
    if reference_shares.shape != comparison_shares.shape:
        raise ValueError("The two share lists must have the same length.")
    return float(np.sum((comparison_shares - reference_shares) * np.log(comparison_shares / reference_shares)))


def population_stability_index(reference_values, comparison_values, bin_count=PSI_BIN_COUNT):
    """Return the PSI of a comparison sample against a reference sample (bins from the reference)."""
    edges = make_bin_edges(reference_values, bin_count)
    return psi_from_shares(count_shares(reference_values, edges), count_shares(comparison_values, edges))


def describe_psi(psi):
    """Return 'ok' (below 0.1), 'watch' (0.1 to 0.2) or 'alert' (above 0.2)."""
    if psi > PSI_ALERT_LEVEL:
        return "alert"
    if psi >= PSI_WATCH_LEVEL:
        return "watch"
    return "ok"


def ks_two_sample(reference_values, comparison_values):
    """Return the two-sample KS statistic D and its p-value."""
    result = stats.ks_2samp(np.asarray(reference_values, dtype=float),
                            np.asarray(comparison_values, dtype=float))
    return {"ks_statistic": float(result.statistic), "ks_p_value": float(result.pvalue)}


def compare_to_reference(reference_values, comparison_values, edges):
    """Compare one numeric feature with the reference: PSI, its level, KS and the means."""
    psi = psi_from_shares(count_shares(reference_values, edges), count_shares(comparison_values, edges))
    return {
        "psi": psi,
        "psi_level": describe_psi(psi),
        **ks_two_sample(reference_values, comparison_values),
        "reference_rows": len(reference_values),
        "comparison_rows": len(comparison_values),
        "reference_mean": float(np.mean(reference_values)),
        "comparison_mean": float(np.mean(comparison_values)),
    }


def hour_profile_shares(table):
    """Return the share of all requests in each New York clock hour 0-23 (a list of 24 shares)."""
    totals = table.groupby("hour_nyc")["request_count"].sum().reindex(range(HOURS_PER_DAY), fill_value=0.0)
    if totals.sum() <= 0:
        raise ValueError("No requests in the table, so there is no hour-of-day profile.")
    return (totals / totals.sum()).to_numpy(dtype=float)


def compare_hour_profiles(reference_table, comparison_table):
    """Compare two hour-of-day demand profiles: PSI over the 24 hours and the largest curve gap."""
    reference_shares = hour_profile_shares(reference_table)
    comparison_shares = hour_profile_shares(comparison_table)
    psi = psi_from_shares(reference_shares, comparison_shares)
    largest_gap = float(np.max(np.abs(np.cumsum(reference_shares) - np.cumsum(comparison_shares))))
    return {"psi": psi, "psi_level": describe_psi(psi), "ks_statistic": largest_gap,
            "ks_p_value": None, "reference_rows": len(reference_table),
            "comparison_rows": len(comparison_table), "reference_mean": None,
            "comparison_mean": None}


def add_week_start(table):
    """Return a copy with week_start: the Monday of each row's New York date."""
    result = table.copy()
    dates = pd.to_datetime(result["event_date_nyc"]).dt.normalize()
    result["week_start"] = dates - pd.to_timedelta(dates.dt.day_of_week, unit="D")
    return result


def score_weekly(scored_rows, method_columns):
    """Score every method in every week: rows, days, MAE, WAPE and bias (percent of actual)."""
    with_weeks = add_week_start(scored_rows)
    records = []
    for week_start, week_rows in with_weeks.groupby("week_start"):
        days = int(week_rows["event_date_nyc"].nunique())
        for method in method_columns:
            scores = score_forecast(week_rows["request_count"], week_rows[method])
            actual_total = float(week_rows["request_count"].sum())
            bias_percent = 100 * (float(week_rows[method].sum()) - actual_total) / actual_total
            records.append({"week_start": week_start, "method": method, "days": days,
                            "rows": scores["rows"], "MAE": scores["MAE"],
                            "WAPE_percent": scores["WAPE_percent"], "bias_percent": bias_percent})
    return pd.DataFrame(records)


def score_groups(scored_rows, group_columns, method_columns):
    """Score every method inside every group (for example window x zone group).

    Returns one row per group and method with rows, MAE, WAPE and bias (percent of actual;
    positive = the method predicted more than actually happened).
    """
    records = []
    for group_values, group_rows in scored_rows.groupby(group_columns, observed=True):
        group_values = group_values if isinstance(group_values, tuple) else (group_values,)
        actual_total = float(group_rows["request_count"].sum())
        for method in method_columns:
            scores = score_forecast(group_rows["request_count"], group_rows[method])
            bias_percent = 100 * (float(group_rows[method].sum()) - actual_total) / actual_total
            records.append({**dict(zip(group_columns, group_values, strict=True)), "method": method,
                            "rows": scores["rows"], "MAE": scores["MAE"],
                            "WAPE_percent": scores["WAPE_percent"], "bias_percent": bias_percent})
    return pd.DataFrame(records)


def find_validation_level(validation_rows, method_columns):
    """Return each method's WAPE and MAE over the validation rows (October 2024)."""
    if validation_rows.empty or (validation_rows["period_label"] != VALIDATION_LABEL).any():
        raise ValueError("Validation level needs rows labelled 'validation', and only those.")
    check_no_drift_rows(validation_rows, "find_validation_level")
    levels = {}
    for method in method_columns:
        scores = score_forecast(validation_rows["request_count"], validation_rows[method])
        levels[method] = {"WAPE_percent": scores["WAPE_percent"], "MAE": scores["MAE"],
                          "rows": scores["rows"]}
    return levels


def mark_weeks_over_limit(weekly_wape_percent, validation_wape_percent,
                          relative_limit=WAPE_RELATIVE_LIMIT):
    """Return a list of True/False: is each week's WAPE more than relative_limit above validation?"""
    limit_percent = validation_wape_percent * (1 + relative_limit)
    return [bool(value > limit_percent) for value in weekly_wape_percent]


def find_alert_weeks(is_over_limit, consecutive_needed=CONSECUTIVE_WEEKS_FOR_ALERT):
    """Return a list of True/False: True where a run of consecutive over-limit weeks reaches the needed length.

    The alert fires at the week that completes the run, and stays on while the run goes on.
    """
    alerts = []
    run_length = 0
    for week_is_over in is_over_limit:
        run_length = run_length + 1 if week_is_over else 0
        alerts.append(run_length >= consecutive_needed)
    return alerts


def list_congestion_zone_ids(zones):
    """Return the ids of Manhattan zones whose centroid is south of the 60th Street approximation."""
    inside = (zones["borough"] == CONGESTION_BOROUGH) & (zones["centroid_latitude"] < CONGESTION_MAX_LATITUDE)
    zone_ids = sorted(zones.loc[inside, "zone_id"].tolist())
    if not zone_ids:
        raise ValueError("No zones fall inside the congestion zone rule.")
    return zone_ids


def list_borderline_zones(zones):
    """Return the Manhattan zones whose centroid is close to the line, so the rule is least certain."""
    near_line = (zones["centroid_latitude"] - CONGESTION_MAX_LATITUDE).abs() <= BORDERLINE_LATITUDE_MARGIN
    return zones[(zones["borough"] == CONGESTION_BOROUGH) & near_line].sort_values("centroid_latitude")


def sum_daily_requests(civil_rows, congestion_zone_ids):
    """Return daily total requests for the congestion-zone group and for all other zones."""
    rows = civil_rows[civil_rows["request_count"].notna()].copy()
    rows["date"] = pd.to_datetime(rows["event_date_nyc"]).dt.normalize()
    rows["group"] = np.where(rows["zone_id"].isin(congestion_zone_ids), "congestion", "rest")
    daily = rows.groupby(["date", "group"])["request_count"].sum().unstack("group")
    return daily[["congestion", "rest"]]


def make_daily_baselines(daily_requests, days):
    """Return, for each given day, the actual totals and two baselines for both zone groups.

    last_year baseline: the same weekday 364 days earlier. december baseline: the mean of the same
    weekday in 2-22 December 2024. A missing baseline day is an error, not a zero.
    """
    december_days = pd.date_range(DECEMBER_BASELINE_FIRST_DAY, DECEMBER_BASELINE_LAST_DAY)
    december_by_weekday = daily_requests.loc[december_days].groupby(december_days.day_of_week).mean()
    records = []
    for day in days:
        last_year_day = day - pd.Timedelta(days=SAME_WEEKDAY_LAST_YEAR_DAYS)
        if day not in daily_requests.index or last_year_day not in daily_requests.index:
            raise ValueError(f"Missing daily totals for {day.date()} or {last_year_day.date()}.")
        record = {"date": day}
        for group in daily_requests.columns:
            record[f"actual_{group}"] = daily_requests.loc[day, group]
            record[f"last_year_{group}"] = daily_requests.loc[last_year_day, group]
            record[f"december_{group}"] = december_by_weekday.loc[day.day_of_week, group]
        records.append(record)
    return pd.DataFrame(records).set_index("date")


def make_window_index(daily_baselines, first_day, last_day):
    """Return demand index (actual / baseline) for both zone groups over a window of days.

    Index 1.0 = same as the baseline. relative_* = congestion-zone index / other-zones index.
    """
    window = daily_baselines.loc[pd.Timestamp(first_day):pd.Timestamp(last_day)]
    if window.empty:
        raise ValueError("No days in the window.")
    result = {"days": len(window)}
    for baseline in ("last_year", "december"):
        indexes = {group: window[f"actual_{group}"].sum() / window[f"{baseline}_{group}"].sum()
                   for group in ("congestion", "rest")}
        result[f"{baseline}_index_congestion"] = float(indexes["congestion"])
        result[f"{baseline}_index_rest"] = float(indexes["rest"])
        result[f"{baseline}_relative"] = float(indexes["congestion"] / indexes["rest"])
    return result


def describe_alert_weeks(weekly_table, alert_flags):
    """Return the first alert week (Monday date as text) and the number of alert weeks."""
    alert_weeks = weekly_table.loc[alert_flags, "week_start"]
    first_week = alert_weeks.min().date().isoformat() if len(alert_weeks) else None
    return first_week, len(alert_weeks)


def write_plain_summary(summary):
    """Write the plain-English 'did the monitor fire, when, how strongly' paragraph.

    The summary is the dictionary built by notebooks/07_drift_check.py. Every number in the
    text comes from it; the wording never claims a cause.
    """
    data_alerts = summary["data_drift_alert_features_january"]
    error = summary["error_monitor_frozen_model"]
    sentences = []
    if data_alerts:
        worst = summary["data_drift_strongest_feature_january"]
        sentences.append(
            f"The data-drift monitor fired for January 2025: {', '.join(data_alerts)} had PSI above "
            f"{PSI_ALERT_LEVEL}; the strongest was {worst['feature']} (PSI {worst['psi']:.3f}).")
    else:
        sentences.append(
            f"The data-drift monitor did not fire for January 2025: no feature had PSI above {PSI_ALERT_LEVEL} "
            f"(largest was {summary['data_drift_strongest_feature_january']['feature']}, "
            f"PSI {summary['data_drift_strongest_feature_january']['psi']:.3f}).")
    if error["january_alert_first_week"]:
        starts_in_december = pd.Timestamp(error["january_alert_first_week"]) < pd.Timestamp(DRIFT_START_DATE)
        december_note = " (a week that also includes late December days)" if starts_in_december else ""
        sentences.append(
            f"The error monitor fired in January: the frozen model's weekly WAPE was more than "
            f"{int(WAPE_RELATIVE_LIMIT * 100)}% above its October level ({error['validation_wape_percent']:.1f}%, "
            f"limit {error['limit_wape_percent']:.1f}%) for {CONSECUTIVE_WEEKS_FOR_ALERT} weeks in a row. The first alert is the "
            f"week starting {error['january_alert_first_week']}{december_note}; the run of weeks "
            f"over the limit began in the week starting {error['january_alert_run_started_week']}. The highest "
            f"weekly WAPE among weeks ending in January was {error['january_max_weekly_wape_percent']:.1f}%.")
    else:
        sentences.append(
            f"The error monitor did not fire in January: the highest weekly WAPE of the frozen model was "
            f"{error['january_max_weekly_wape_percent']:.1f}% against a limit of {error['limit_wape_percent']:.1f}% "
            f"(October level {error['validation_wape_percent']:.1f}%).")
    if error["weeks_over_limit_after_october"]:
        sentences.append(
            "Weeks over the limit since November (week starting Monday): "
            + ", ".join(error["weeks_over_limit_after_october"]) + ".")
    sentences.append(
        "These are descriptions of how the data and errors changed, not explanations: January 2025 also differs "
        "in season, holidays and weather, and congestion pricing started on 5 January, so no cause is claimed.")
    return " ".join(sentences)
