"""Small synthetic tests for src/demand_features.py (no real data is used).

The most important test proves the forecast cannot peek: changing any count
that is less than 7 days before the target hour, or at/after the target hour,
leaves the features of that target row exactly the same.
"""

import numpy as np
import pandas as pd
import pytest

from src.demand_features import (
    LAG_COLUMNS,
    PRETEST_MONTHS,
    add_baseline_columns,
    build_features,
    choose_method_from_folds,
    build_frozen_profiles,
    find_repo_root,
    fit_count_scale,
    list_feature_columns,
    make_civil_hour_table,
    make_walk_forward_folds,
    mark_common_rows,
    score_by_group,
    score_forecast,
)

FIRST_DAY = "2024-06-03"
DAYS_IN_TABLE = 42
ZONE_IDS = [1, 2]
BOROUGHS = ["Bronx", "Queens"]
RANDOM_SEED = 7


def make_zone_hour_rows(days=DAYS_IN_TABLE, seed=RANDOM_SEED):
    """Make random hourly counts for two zones over a no-clock-change stretch."""
    random_generator = np.random.default_rng(seed)
    dates = pd.date_range(FIRST_DAY, periods=days, freq="D")
    rows = pd.MultiIndex.from_product(
        [ZONE_IDS, dates, range(24)], names=["zone_id", "event_date_nyc", "hour_nyc"]
    ).to_frame(index=False)
    rows["request_count"] = random_generator.integers(0, 500, size=len(rows)).astype(float)
    rows["is_federal_holiday"] = False
    rows["period_label"] = "train"
    return rows


def make_zones():
    """Make a two-zone zones table."""
    return pd.DataFrame({
        "zone_id": ZONE_IDS, "borough": BOROUGHS,
        "centroid_latitude": [40.8, 40.7], "centroid_longitude": [-73.9, -73.8],
        "is_airport": [False, True],
    })


def target_timestamp(table):
    """Return each row's civil clock time as one timestamp (date plus hour)."""
    return table["event_date_nyc"] + pd.to_timedelta(table["hour_nyc"], unit="h")


def test_lags_are_the_same_zone_and_hour_7_to_28_days_earlier():
    civil = make_civil_hour_table(make_zone_hour_rows())
    features = build_features(civil, make_zones(), BOROUGHS)
    target = features[(features["zone_id"] == 2) & (features["event_date_nyc"] == "2024-07-10")
                      & (features["hour_nyc"] == 17)].iloc[0]
    for days_back, column in zip([7, 14, 21, 28], LAG_COLUMNS, strict=True):
        source_day = pd.Timestamp("2024-07-10") - pd.Timedelta(days=days_back)
        expected = civil[(civil["zone_id"] == 2) & (civil["event_date_nyc"] == source_day)
                         & (civil["hour_nyc"] == 17)]["request_count"].iloc[0]
        assert target[column] == expected
    assert target["lag_mean_4w"] == pytest.approx(target[LAG_COLUMNS].mean())


def test_missing_history_stays_missing_and_mean_needs_two_lags():
    civil = make_civil_hour_table(make_zone_hour_rows())
    features = build_features(civil, make_zones(), BOROUGHS)
    day_of = features["event_date_nyc"]
    first_day = features[day_of == FIRST_DAY]
    assert first_day[LAG_COLUMNS].isna().all().all()
    assert first_day["lag_mean_4w"].isna().all()
    second_week = features[day_of == "2024-06-10"]
    assert second_week["lag_7d"].notna().all()
    assert second_week["lag_14d"].isna().all()
    assert second_week["lag_mean_4w"].isna().all()
    third_week = features[day_of == "2024-06-17"]
    assert third_week["lag_mean_4w"].notna().all()
    assert third_week["lag_21d"].isna().all()


def test_features_ignore_everything_less_than_7_days_before_or_after_the_target():
    zone_hour_rows = make_zone_hour_rows()
    civil = make_civil_hour_table(zone_hour_rows)
    original = build_features(civil, make_zones(), BOROUGHS)
    feature_columns = list_feature_columns(BOROUGHS)
    time_of_row = target_timestamp(civil)
    for target_text in ["2024-07-01 08:00", "2024-07-08 00:00", "2024-07-14 23:00"]:
        target_time = pd.Timestamp(target_text)
        recent_or_later = time_of_row > target_time - pd.Timedelta(days=7)
        assert recent_or_later.sum() > 0
        changed_civil = civil.copy()
        changed_civil.loc[recent_or_later, "request_count"] = 99999.0
        changed = build_features(changed_civil, make_zones(), BOROUGHS)
        is_target = (target_timestamp(original) == target_time)
        assert is_target.sum() == len(ZONE_IDS)
        pd.testing.assert_frame_equal(
            original.loc[is_target, feature_columns].reset_index(drop=True),
            changed.loc[is_target, feature_columns].reset_index(drop=True),
        )


def test_changing_an_older_value_does_change_the_matching_lag():
    civil = make_civil_hour_table(make_zone_hour_rows())
    original = build_features(civil, make_zones(), BOROUGHS)
    changed_civil = civil.copy()
    seven_days_before = ((changed_civil["zone_id"] == 1)
                         & (target_timestamp(changed_civil) == pd.Timestamp("2024-07-01 08:00")))
    changed_civil.loc[seven_days_before, "request_count"] = 12345.0
    changed = build_features(changed_civil, make_zones(), BOROUGHS)
    target = (changed["zone_id"] == 1) & (target_timestamp(changed) == pd.Timestamp("2024-07-08 08:00"))
    assert changed.loc[target, "lag_7d"].iloc[0] == 12345.0
    assert original.loc[target, "lag_7d"].iloc[0] != 12345.0


def test_clock_change_duplicate_hour_is_averaged_or_kept_null():
    rows = pd.DataFrame({
        "zone_id": [1, 1, 2, 2, 3, 3],
        "event_date_nyc": pd.to_datetime(["2024-11-03"] * 6),
        "hour_nyc": [1] * 6,
        "request_count": [10.0, 20.0, np.nan, np.nan, 5.0, np.nan],
        "is_federal_holiday": False,
        "period_label": "test",
    })
    civil = make_civil_hour_table(rows).set_index("zone_id")
    assert len(civil) == 3
    assert civil.loc[1, "request_count"] == 15.0
    assert np.isnan(civil.loc[2, "request_count"])
    assert np.isnan(civil.loc[3, "request_count"])


def test_missing_columns_fail_loudly():
    with pytest.raises(ValueError, match="missing columns"):
        make_civil_hour_table(pd.DataFrame({"zone_id": [1]}))


def test_calendar_and_geography_features():
    civil = make_civil_hour_table(make_zone_hour_rows(days=14))
    features = build_features(civil, make_zones(), BOROUGHS)
    monday_six = features[(features["event_date_nyc"] == "2024-06-03") & (features["hour_nyc"] == 6)]
    assert (monday_six["weekday"] == 0).all()
    assert (monday_six["month"] == 6).all()
    assert (monday_six["is_weekend"] == 0).all()
    assert monday_six["hour_sin"].iloc[0] == pytest.approx(1.0)
    saturday = features[features["event_date_nyc"] == "2024-06-08"]
    assert (saturday["is_weekend"] == 1).all()
    queens = features[features["zone_id"] == 2].iloc[0]
    assert queens["is_airport"] == 1.0
    assert queens["borough_Queens"] == 1.0
    assert queens["borough_Bronx"] == 0.0


def test_unknown_zone_or_borough_fails_loudly():
    civil = make_civil_hour_table(make_zone_hour_rows(days=8))
    with pytest.raises(ValueError, match="zones table"):
        build_features(civil, make_zones().iloc[:1], BOROUGHS)
    with pytest.raises(ValueError, match="Unexpected boroughs"):
        build_features(civil, make_zones(), ["Bronx"])


def test_frozen_profiles_use_only_days_before_the_end_date():
    civil = make_civil_hour_table(make_zone_hour_rows())
    end_date = "2024-07-01"
    profiles = build_frozen_profiles(civil, end_date)
    changed_civil = civil.copy()
    changed_civil.loc[changed_civil["event_date_nyc"] >= end_date, "request_count"] = 99999.0
    changed_profiles = build_frozen_profiles(changed_civil, end_date)
    pd.testing.assert_frame_equal(profiles, changed_profiles)
    one_zone = civil[(civil["zone_id"] == 1) & (civil["hour_nyc"] == 9)].copy()
    one_zone["weekday"] = one_zone["event_date_nyc"].dt.dayofweek
    monday = one_zone[(one_zone["weekday"] == 0) & (one_zone["event_date_nyc"] < end_date)]
    last_four = monday.sort_values("event_date_nyc").tail(4)["request_count"].mean()
    last_one = monday.sort_values("event_date_nyc").tail(1)["request_count"].iloc[0]
    row = profiles[(profiles["zone_id"] == 1) & (profiles["weekday"] == 0) & (profiles["hour_nyc"] == 9)]
    assert row["frozen_profile_28d"].iloc[0] == pytest.approx(last_four)
    assert row["frozen_profile_7d"].iloc[0] == pytest.approx(last_one)


def test_frozen_profile_is_missing_when_history_is_short():
    civil = make_civil_hour_table(make_zone_hour_rows(days=20))
    profiles = build_frozen_profiles(civil, "2024-06-23")
    assert profiles["frozen_profile_28d"].isna().all()
    assert profiles["frozen_profile_7d"].notna().all()


def test_baseline_columns_and_common_rows():
    civil = make_civil_hour_table(make_zone_hour_rows())
    features = build_features(civil, make_zones(), BOROUGHS)
    profiles = build_frozen_profiles(civil, "2024-07-01")
    table = add_baseline_columns(features, profiles)
    assert len(table) == len(features)
    assert table["pred_last_week"].equals(table["lag_7d"])
    four_weeks_known = table[LAG_COLUMNS].notna().all(axis=1)
    assert table.loc[four_weeks_known, "pred_four_week_mean"].notna().all()
    assert table.loc[~four_weeks_known, "pred_four_week_mean"].isna().all()
    columns = ["pred_last_week", "pred_four_week_mean", "pred_frozen_profile"]
    common = mark_common_rows(table, columns)
    assert common.sum() == (four_weeks_known & table["pred_frozen_profile"].notna()).sum()


def test_score_forecast_values():
    scores = score_forecast([10, 20, 30], [12, 18, 30])
    assert scores["rows"] == 3
    assert scores["MAE"] == pytest.approx(4 / 3)
    assert scores["RMSE"] == pytest.approx(np.sqrt(8 / 3))
    assert scores["WAPE_percent"] == pytest.approx(100 * 4 / 60)


def test_score_forecast_rejects_missing_values_and_length_mismatch():
    with pytest.raises(ValueError, match="missing"):
        score_forecast([1, 2], [1, np.nan])
    with pytest.raises(ValueError, match="same length"):
        score_forecast([1, 2], [1])


def test_score_by_group_and_count_scale():
    table = pd.DataFrame({"request_count": [10.0, 20.0, 30.0, 40.0], "pred": [11.0, 19.0, 33.0, 40.0],
                          "group": ["a", "a", "b", "b"]})
    scores = score_by_group(table, "group", ["pred"]).set_index("group")
    assert scores.loc["a", "MAE"] == 1.0
    assert scores.loc["b", "MAE"] == 1.5
    assert fit_count_scale([10, 10], [5, 5]) == 2.0
    with pytest.raises(ValueError, match="zero"):
        fit_count_scale([1], [0])


def test_walk_forward_folds_for_the_pretest_months():
    folds = make_walk_forward_folds(PRETEST_MONTHS)
    assert len(folds) == 7
    assert folds[0]["train_months"] == ["2024-01", "2024-02", "2024-03"]
    assert folds[0]["validation_month"] == "2024-04"
    assert folds[-1]["train_months"] == PRETEST_MONTHS[:9]
    assert folds[-1]["validation_month"] == "2024-10"


def test_walk_forward_folds_never_train_on_the_future():
    for fold in make_walk_forward_folds(PRETEST_MONTHS):
        assert max(fold["train_months"]) < fold["validation_month"]
        assert fold["validation_month"] not in fold["train_months"]


def test_walk_forward_folds_never_touch_the_test_months():
    used_months = set()
    for fold in make_walk_forward_folds(PRETEST_MONTHS):
        used_months.update(fold["train_months"] + [fold["validation_month"]])
    assert max(used_months) == "2024-10"
    assert "2024-11" not in used_months and "2024-12" not in used_months


def test_walk_forward_folds_reject_bad_month_lists():
    with pytest.raises(ValueError, match="need more than"):
        make_walk_forward_folds(["2024-01", "2024-02", "2024-03"])
    with pytest.raises(ValueError, match="consecutive"):
        make_walk_forward_folds(["2024-01", "2024-02", "2024-04", "2024-05"])
    with pytest.raises(ValueError, match="consecutive"):
        make_walk_forward_folds(["2024-04", "2024-03", "2024-02", "2024-01"])
    with pytest.raises(ValueError, match="consecutive"):
        make_walk_forward_folds(["2024-01", "2024-02", "2024-02", "2024-03"])


FOLD_NAMES = [f"fold{number}" for number in range(1, 8)]


def make_fold_scores(model_wape, baseline_wape):
    """Make a fold-score table with one model and one baseline (WAPE per fold)."""
    return pd.DataFrame([model_wape, baseline_wape], index=["model", "baseline"], columns=FOLD_NAMES)


def test_choose_method_picks_model_that_wins_most_folds():
    scores = make_fold_scores([10, 10, 10, 10, 10, 10, 10], [12, 12, 12, 12, 12, 12, 12])
    choice = choose_method_from_folds(scores, ["model"], ["baseline"])
    assert choice["chosen"] == "model" and choice["model_is_chosen"]
    assert choice["folds_model_wins"] == 7 and choice["number_of_folds"] == 7


def test_choose_method_keeps_baseline_when_model_wins_too_few_folds():
    # The model is better on average (one huge win) but wins only 4 of 7 folds.
    scores = make_fold_scores([5, 11, 11, 11, 11, 11, 11], [12, 10, 10, 10, 12, 12, 12])
    choice = choose_method_from_folds(scores, ["model"], ["baseline"])
    assert scores.loc["model"].mean() < scores.loc["baseline"].mean()
    assert choice["folds_model_wins"] == 4
    assert choice["chosen"] == "baseline" and not choice["model_is_chosen"]


def test_choose_method_keeps_baseline_when_model_is_worse_on_average():
    scores = make_fold_scores([13, 13, 13, 13, 13, 13, 13], [12, 12, 12, 12, 12, 12, 12])
    choice = choose_method_from_folds(scores, ["model"], ["baseline"])
    assert choice["chosen"] == "baseline"
    assert choice["worst_fold_wape"] == 12


def test_choose_method_finds_best_model_and_best_baseline_by_mean():
    scores = pd.DataFrame(
        [[9] * 7, [10] * 7, [12] * 7, [11] * 7],
        index=["model_a", "model_b", "baseline_a", "baseline_b"], columns=FOLD_NAMES)
    choice = choose_method_from_folds(scores, ["model_a", "model_b"], ["baseline_a", "baseline_b"])
    assert choice["best_model"] == "model_a" and choice["best_baseline"] == "baseline_b"
    assert choice["chosen"] == "model_a"


def test_choose_method_rejects_missing_methods_and_missing_scores():
    scores = make_fold_scores([10] * 7, [12] * 7)
    with pytest.raises(ValueError, match="missing methods"):
        choose_method_from_folds(scores, ["model"], ["no_such_baseline"])
    with pytest.raises(ValueError, match="at least one"):
        choose_method_from_folds(scores, [], ["baseline"])
    scores.loc["model", "fold3"] = np.nan
    with pytest.raises(ValueError, match="no missing values"):
        choose_method_from_folds(scores, ["model"], ["baseline"])


def test_find_repo_root_finds_this_repository():
    root = find_repo_root()
    assert (root / "repo_instructions.md").is_file()
