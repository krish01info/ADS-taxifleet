"""Check the Phase 6 statistics helpers on small invented numbers with known answers."""

import numpy as np
import pytest
from statsmodels.stats.multitest import multipletests

from src import hypothesis_tests as ht

BOOTSTRAP_SEED = 7
OTHER_BOOTSTRAP_SEED = 8
EIGHT_POSITIVE_DIFFERENCES = np.arange(1.0, 9.0)
EXACT_ONE_SIDED_P_FOR_EIGHT_POSITIVES = 1 / 2**8
EXACT_TWO_SIDED_P_FOR_EIGHT_POSITIVES = 2 / 2**8
SMALL_P = 0.001
LARGE_P = 0.4
LARGE_GAIN_PERCENT = 3.0
TINY_GAIN_PERCENT = 0.5


def test_holm_matches_hand_calculation():
    adjusted = ht.holm_adjust([0.01, 0.04, 0.03, 0.005])
    assert adjusted == pytest.approx([0.03, 0.06, 0.06, 0.02])


def test_holm_matches_statsmodels_and_caps_at_one():
    raw_p_values = [0.9, 0.2, 0.049, 0.5, 0.0001]
    expected = multipletests(raw_p_values, method="holm")[1]
    assert ht.holm_adjust(raw_p_values) == pytest.approx(expected)
    assert ht.holm_adjust([0.8, 0.9]).max() <= 1.0


def test_holm_never_lowers_a_p_value_and_rejects_bad_input():
    raw_p_values = np.array([0.02, 0.2, 0.6])
    assert (ht.holm_adjust(raw_p_values) >= raw_p_values).all()
    with pytest.raises(ValueError):
        ht.holm_adjust([0.5, 1.5])
    with pytest.raises(ValueError):
        ht.holm_adjust([])


def test_wilcoxon_exact_p_values_for_eight_positive_differences():
    _, p_one_sided = ht.paired_wilcoxon(EIGHT_POSITIVE_DIFFERENCES, "greater")
    _, p_two_sided = ht.paired_wilcoxon(EIGHT_POSITIVE_DIFFERENCES, "two-sided")
    assert p_one_sided == pytest.approx(EXACT_ONE_SIDED_P_FOR_EIGHT_POSITIVES)
    assert p_two_sided == pytest.approx(EXACT_TWO_SIDED_P_FOR_EIGHT_POSITIVES)


def test_wilcoxon_wrong_direction_gives_large_one_sided_p():
    _, p_one_sided = ht.paired_wilcoxon(-EIGHT_POSITIVE_DIFFERENCES, "greater")
    assert p_one_sided > 0.99


def test_wilcoxon_with_all_ties_gives_p_of_one():
    statistic, p_value = ht.paired_wilcoxon(np.zeros(10), "greater")
    assert statistic == 0.0
    assert p_value == 1.0


def test_effect_sizes():
    assert ht.rank_biserial_effect(EIGHT_POSITIVE_DIFFERENCES) == 1.0
    assert ht.rank_biserial_effect(-EIGHT_POSITIVE_DIFFERENCES) == -1.0
    assert ht.rank_biserial_effect([1.0, -1.0]) == 0.0
    assert ht.rank_biserial_effect(np.zeros(4)) == 0.0
    assert ht.cohens_dz([1.0, 2.0, 3.0]) == pytest.approx(2.0)
    assert ht.cohens_dz([2.0, 2.0, 2.0]) == 0.0


def test_bootstrap_is_reproducible_with_the_seed():
    differences = np.random.default_rng(1).normal(2.0, 1.0, size=40)
    first_interval = ht.bootstrap_median_ci(differences, BOOTSTRAP_SEED, resamples=500)
    second_interval = ht.bootstrap_median_ci(differences, BOOTSTRAP_SEED, resamples=500)
    other_interval = ht.bootstrap_median_ci(differences, OTHER_BOOTSTRAP_SEED, resamples=500)
    assert first_interval == second_interval
    assert first_interval != other_interval
    lower, upper = first_interval
    assert lower < np.median(differences) < upper
    assert lower > 0


def test_bootstrap_of_constant_differences_is_that_constant():
    lower, upper = ht.bootstrap_median_ci(np.full(10, 3.0), BOOTSTRAP_SEED, resamples=100)
    assert lower == upper == 3.0


def test_relative_gain_uses_size_of_second_plan_profit():
    assert ht.relative_gain_percent(0.5, 50.0) == pytest.approx(1.0)
    assert ht.relative_gain_percent(0.5, -50.0) == pytest.approx(1.0)
    assert np.isnan(ht.relative_gain_percent(0.5, 0.0))


def test_interval_side():
    assert ht.interval_is_mostly_wrong_side(-3.0, -1.0)
    assert ht.interval_is_mostly_wrong_side(-3.0, 1.0)
    assert not ht.interval_is_mostly_wrong_side(-1.0, 3.0)
    assert not ht.interval_is_mostly_wrong_side(1.0, 3.0)


def test_classify_fleet_size_branches():
    assert ht.classify_fleet_size(SMALL_P, 2.0, LARGE_GAIN_PERCENT, 1.0, 3.0) == ht.STATUS_PASS
    assert ht.classify_fleet_size(SMALL_P, 0.0, 0.0, -1.0, 1.0) == ht.STATUS_WRONG
    assert ht.classify_fleet_size(SMALL_P, -2.0, -LARGE_GAIN_PERCENT, -3.0, -1.0) == ht.STATUS_WRONG
    assert ht.classify_fleet_size(LARGE_P, 0.2, LARGE_GAIN_PERCENT, -3.0, 1.0) == ht.STATUS_WRONG
    assert ht.classify_fleet_size(LARGE_P, 1.0, LARGE_GAIN_PERCENT, -1.0, 3.0) == ht.STATUS_MIXED
    assert ht.classify_fleet_size(SMALL_P, 0.1, TINY_GAIN_PERCENT, 0.05, 0.2) == ht.STATUS_MIXED
    assert ht.classify_fleet_size(SMALL_P, 1.0, float("nan"), 0.5, 1.5) == ht.STATUS_MIXED


def test_verdict_for_each_branch():
    passing, wrong, mixed = ht.STATUS_PASS, ht.STATUS_WRONG, ht.STATUS_MIXED
    assert ht.decide_verdict([passing, passing, passing]) == ht.VERDICT_SUPPORTED
    assert ht.decide_verdict([passing, mixed, passing]) == ht.VERDICT_SUPPORTED
    assert ht.decide_verdict([wrong, wrong, wrong]) == ht.VERDICT_NOT_SUPPORTED
    assert ht.decide_verdict([wrong, passing, wrong]) == ht.VERDICT_NOT_SUPPORTED
    assert ht.decide_verdict([passing, wrong, mixed]) == ht.VERDICT_INCONCLUSIVE
    assert ht.decide_verdict([mixed, mixed, mixed]) == ht.VERDICT_INCONCLUSIVE
    assert ht.decide_verdict([passing, mixed, mixed]) == ht.VERDICT_INCONCLUSIVE
    with pytest.raises(ValueError):
        ht.decide_verdict([])


def test_compare_two_plans_on_clear_gain_and_on_identical_plans():
    noise = np.random.default_rng(3).normal(0.0, 0.2, size=30)
    second_plan = 50.0 + np.random.default_rng(4).normal(0.0, 3.0, size=30)
    first_plan = second_plan + 1.5 + noise
    row = ht.compare_two_plans(first_plan, second_plan, BOOTSTRAP_SEED)
    assert row["n_days"] == 30
    assert row["median_difference_usd"] == pytest.approx(1.5, abs=0.2)
    assert row["relative_gain_percent"] > ht.MIN_RELATIVE_GAIN_PERCENT
    assert row["p_one_sided"] < 0.001
    assert row["win_share"] == 1.0
    assert row["ci95_lower_usd"] > 0
    same_row = ht.compare_two_plans(second_plan, second_plan, BOOTSTRAP_SEED)
    assert same_row["median_difference_usd"] == 0.0
    assert same_row["p_one_sided"] == 1.0
    assert same_row["tie_share"] == 1.0


def test_paired_differences_reject_bad_lists():
    with pytest.raises(ValueError):
        ht.make_paired_differences([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        ht.make_paired_differences([1.0, np.nan], [1.0, 2.0])
    with pytest.raises(ValueError):
        ht.make_paired_differences([1.0], [1.0])
