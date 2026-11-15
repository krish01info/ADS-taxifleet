"""Small synthetic tests for src/drift_monitor.py (no real data is used).

They check: PSI is zero for identical samples and large for shifted ones, the KS helper, the
alert rule (two weeks in a row), the weekly scoring, the congestion-zone rule, the demand index,
and that nothing can be fitted on January 2025.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import drift_monitor as monitor

RANDOM_SEED = 11
SAMPLE_SIZE = 5000
SHIFT_IN_STANDARD_DEVIATIONS = 1.0


def make_normal_sample(mean=0.0, size=SAMPLE_SIZE, seed=RANDOM_SEED):
    """Return a seeded sample from a normal curve with the given mean."""
    return np.random.default_rng(seed).normal(loc=mean, scale=1.0, size=size)


def make_period_rows(first_day, days, label, count=10.0, zone_ids=(1, 2)):
    """Make one row per zone, day and hour with a constant request count."""
    dates = pd.date_range(first_day, periods=days)
    rows = pd.MultiIndex.from_product([zone_ids, dates, range(24)],
                                      names=["zone_id", "event_date_nyc", "hour_nyc"]).to_frame(index=False)
    rows["request_count"] = count
    rows["period_label"] = label
    return rows


def test_psi_is_zero_for_identical_samples():
    sample = make_normal_sample()
    assert monitor.population_stability_index(sample, sample.copy()) == pytest.approx(0.0, abs=1e-12)


def test_psi_is_small_for_two_samples_from_the_same_curve():
    first = make_normal_sample(seed=1)
    second = make_normal_sample(seed=2)
    psi = monitor.population_stability_index(first, second)
    assert psi < monitor.PSI_WATCH_LEVEL
    assert monitor.describe_psi(psi) == "ok"


def test_psi_is_large_for_a_shifted_sample():
    reference = make_normal_sample()
    shifted = make_normal_sample(mean=SHIFT_IN_STANDARD_DEVIATIONS)
    psi = monitor.population_stability_index(reference, shifted)
    assert psi > monitor.PSI_ALERT_LEVEL
    assert monitor.describe_psi(psi) == "alert"


def test_psi_levels_have_the_declared_boundaries():
    assert monitor.describe_psi(0.0999) == "ok"
    assert monitor.describe_psi(0.1) == "watch"
    assert monitor.describe_psi(0.2) == "watch"
    assert monitor.describe_psi(0.2001) == "alert"


def test_psi_handles_many_repeated_values_and_empty_bins():
    reference = np.array([0.0] * 80 + [1.0] * 15 + [2.0] * 5)
    comparison = np.array([5.0] * 100)
    edges = monitor.make_bin_edges(reference)
    assert monitor.count_shares(reference, edges).sum() == pytest.approx(1.0)
    assert monitor.population_stability_index(reference, comparison) > monitor.PSI_ALERT_LEVEL


def test_bin_helpers_fail_loudly_on_bad_input():
    with pytest.raises(ValueError):
        monitor.make_bin_edges(np.array([]))
    with pytest.raises(ValueError):
        monitor.make_bin_edges(np.array([1.0, np.nan]))
    with pytest.raises(ValueError):
        monitor.count_shares(np.array([1.0, np.nan]), np.array([0.5]))
    with pytest.raises(ValueError):
        monitor.psi_from_shares([0.5, 0.5], [1.0])


def test_ks_helper_identical_and_shifted():
    sample = make_normal_sample()
    same = monitor.ks_two_sample(sample, sample.copy())
    assert same["ks_statistic"] == 0.0
    assert same["ks_p_value"] == pytest.approx(1.0)
    shifted = monitor.ks_two_sample(sample, make_normal_sample(mean=SHIFT_IN_STANDARD_DEVIATIONS))
    assert shifted["ks_statistic"] > 0.3
    assert shifted["ks_p_value"] < 1e-6


def test_hour_profile_psi_is_zero_for_the_same_profile_and_large_for_a_moved_peak():
    morning = make_period_rows("2024-03-01", 2, "train")
    morning["request_count"] = np.where(morning["hour_nyc"] < 12, 20.0, 1.0)
    evening = morning.copy()
    evening["request_count"] = np.where(evening["hour_nyc"] < 12, 1.0, 20.0)
    assert monitor.compare_hour_profiles(morning, morning)["psi"] == pytest.approx(0.0, abs=1e-12)
    moved = monitor.compare_hour_profiles(morning, evening)
    assert moved["psi_level"] == "alert"
    assert moved["ks_statistic"] > 0.5


def test_alert_needs_two_weeks_in_a_row():
    over = [False, True, False, True, True, True, False, True]
    assert monitor.find_alert_weeks(over) == [False, False, False, False, True, True, False, False]
    assert monitor.find_alert_weeks([True]) == [False]
    assert monitor.find_alert_weeks([True, True, True], consecutive_needed=3) == [False, False, True]
    assert monitor.find_alert_weeks([]) == []


def test_week_is_over_limit_only_when_more_than_25_percent_above_validation():
    validation_wape = 12.0
    limit = validation_wape * 1.25
    assert monitor.mark_weeks_over_limit([limit - 0.01, limit, limit + 0.01], validation_wape) == [False, False, True]


def test_weekly_scoring_uses_monday_weeks_and_known_numbers():
    rows = make_period_rows("2024-10-07", 14, "validation", count=100.0, zone_ids=(1,))
    rows["pred_model"] = 100.0
    rows.loc[rows["event_date_nyc"] >= pd.Timestamp("2024-10-14"), "pred_model"] = 120.0
    weekly = monitor.score_weekly(rows, ["pred_model"])
    assert weekly["week_start"].tolist() == [pd.Timestamp("2024-10-07"), pd.Timestamp("2024-10-14")]
    assert weekly["days"].tolist() == [7, 7]
    assert weekly["WAPE_percent"].tolist() == pytest.approx([0.0, 20.0])
    assert weekly["bias_percent"].tolist() == pytest.approx([0.0, 20.0])
    assert weekly["MAE"].tolist() == pytest.approx([0.0, 20.0])


def test_week_start_of_a_sunday_is_the_monday_before():
    table = pd.DataFrame({"event_date_nyc": pd.to_datetime(["2024-10-13", "2024-10-14"])})
    assert monitor.add_week_start(table)["week_start"].tolist() == [pd.Timestamp("2024-10-07"),
                                                                    pd.Timestamp("2024-10-14")]


def test_validation_level_accepts_only_validation_rows():
    rows = make_period_rows("2024-10-01", 3, "validation")
    rows["pred_model"] = 12.0
    level = monitor.find_validation_level(rows, ["pred_model"])
    assert level["pred_model"]["WAPE_percent"] == pytest.approx(20.0)
    with pytest.raises(ValueError):
        monitor.find_validation_level(rows.assign(period_label="test"), ["pred_model"])


def test_nothing_can_be_fitted_on_january_2025():
    reference = make_period_rows("2024-03-01", 3, "train")
    assert len(monitor.fit_reference_edges(
        reference.assign(request_count=np.arange(len(reference), dtype=float)), "request_count")) > 0
    with_drift_label = pd.concat([reference, make_period_rows("2024-03-05", 1, "drift")])
    with pytest.raises(ValueError):
        monitor.fit_reference_edges(with_drift_label, "request_count")
    with_january_date = pd.concat([reference, make_period_rows("2025-01-02", 1, "train")])
    with pytest.raises(ValueError):
        monitor.fit_reference_edges(with_january_date, "request_count")
    january_validation = make_period_rows("2025-01-02", 1, "validation")
    january_validation["pred_model"] = 1.0
    with pytest.raises(ValueError):
        monitor.find_validation_level(january_validation, ["pred_model"])


def make_zones():
    """Make a small zones table: two inside the congestion rule, three outside."""
    return pd.DataFrame({
        "zone_id": [1, 2, 3, 4, 5],
        "zone_name": ["south", "midtown", "north", "brooklyn", "far south"],
        "borough": ["Manhattan", "Manhattan", "Manhattan", "Brooklyn", "Queens"],
        "centroid_latitude": [40.70, 40.76, 40.80, 40.70, 40.60],
    })


def test_congestion_zone_rule_needs_manhattan_and_a_southern_centroid():
    assert monitor.list_congestion_zone_ids(make_zones()) == [1, 2]
    with pytest.raises(ValueError):
        monitor.list_congestion_zone_ids(make_zones().assign(borough="Bronx"))


def test_borderline_zones_are_those_close_to_the_line():
    borderline = monitor.list_borderline_zones(make_zones())
    assert borderline["zone_id"].tolist() == [2]


def make_daily_requests():
    """Make two groups with constant daily totals from 1 Jan 2024 to 31 Jan 2025."""
    days = pd.date_range("2024-01-01", "2025-01-31")
    return pd.DataFrame({"congestion": 100.0, "rest": 1000.0}, index=days)


def test_demand_index_is_one_when_nothing_changes_and_shows_a_change():
    daily = make_daily_requests()
    january = pd.date_range("2025-01-01", "2025-01-31")
    baselines = monitor.make_daily_baselines(daily, january)
    unchanged = monitor.make_window_index(baselines, "2025-01-01", "2025-01-31")
    assert unchanged["last_year_index_congestion"] == pytest.approx(1.0)
    assert unchanged["december_relative"] == pytest.approx(1.0)
    daily.loc[january, "congestion"] = 80.0
    changed = monitor.make_window_index(monitor.make_daily_baselines(daily, january), "2025-01-05", "2025-01-31")
    assert changed["last_year_index_congestion"] == pytest.approx(0.8)
    assert changed["last_year_index_rest"] == pytest.approx(1.0)
    assert changed["last_year_relative"] == pytest.approx(0.8)
    assert changed["days"] == 27


def test_missing_baseline_day_is_an_error_not_a_zero():
    daily = make_daily_requests().drop(pd.Timestamp("2024-01-03"))
    with pytest.raises(ValueError):
        monitor.make_daily_baselines(daily, pd.date_range("2025-01-01", "2025-01-03"))


def test_daily_requests_split_into_congestion_and_rest_and_skip_missing_counts():
    rows = make_period_rows("2024-03-01", 1, "train", count=1.0, zone_ids=(1, 3))
    rows.loc[(rows["zone_id"] == 3) & (rows["hour_nyc"] == 0), "request_count"] = np.nan
    daily = monitor.sum_daily_requests(rows, [1])
    assert daily.loc[pd.Timestamp("2024-03-01"), "congestion"] == 24.0
    assert daily.loc[pd.Timestamp("2024-03-01"), "rest"] == 23.0


def test_group_scores_report_bias_sign():
    rows = make_period_rows("2024-10-01", 1, "validation", count=10.0, zone_ids=(1, 2))
    rows["pred_model"] = np.where(rows["zone_id"] == 1, 15.0, 5.0)
    scores = monitor.score_groups(rows, ["zone_id"], ["pred_model"]).set_index("zone_id")
    assert scores.loc[1, "bias_percent"] == pytest.approx(50.0)
    assert scores.loc[2, "bias_percent"] == pytest.approx(-50.0)
    assert scores.loc[1, "WAPE_percent"] == pytest.approx(50.0)


def make_summary(data_alert, error_alert):
    """Make the small dictionary that write_plain_summary reads."""
    return {
        "data_drift_alert_features_january": ["lag_7d"] if data_alert else [],
        "data_drift_strongest_feature_january": {"feature": "lag_7d", "psi": 0.25 if data_alert else 0.01},
        "error_monitor_frozen_model": {
            "weeks_over_limit_after_october": ["2025-01-06"],
            "validation_wape_percent": 12.0, "limit_wape_percent": 15.0,
            "january_alert_first_week": "2025-01-13" if error_alert else None,
            "january_alert_run_started_week": "2025-01-06",
            "january_max_weekly_wape_percent": 20.0,
        },
    }


def test_plain_summary_states_fire_or_not_and_makes_no_causal_claim():
    fired = monitor.write_plain_summary(make_summary(True, True))
    quiet = monitor.write_plain_summary(make_summary(False, False))
    assert "data-drift monitor fired" in fired
    assert "error monitor fired" in fired
    assert "data-drift monitor did not fire" in quiet
    assert "error monitor did not fire" in quiet
    for text in (fired, quiet):
        assert "no cause is claimed" in text
