"""Phase 6 statistics helpers: paired tests, Holm correction, effect sizes, bootstrap, verdict.

Where it fits: notebooks/06_hypothesis_tests.py runs the fleet simulator on the same days for
two plans and gets one daily profit per car for each plan. The functions below compare the two
lists of daily numbers (day i of plan A against day i of plan B) and turn the result into one
table row and one verdict. Nothing here touches the simulator or any data file.

VERDICT RULE (written before the test days were simulated; do not change it after looking)
    The rule is applied to one hypothesis at a time, using the three fleet sizes.
    "First plan" is the plan the hypothesis says is better; "second plan" is what it is
    compared with. The daily difference is first minus second, in USD of profit per car.

    Step 1: a fleet size PASSES when all three hold:
        (a) Holm-adjusted one-sided Wilcoxon p-value < 0.05 (Holm over the four hypotheses
            H2-H5 at that fleet size),
        (b) the median paired difference is positive,
        (c) the relative gain (median difference / median daily profit per car of the
            second plan, in percent) is at least 1 percent.
    Step 2: a fleet size is WRONG when the median difference is zero or negative, or when the
        Holm-adjusted p-value is 0.05 or more and the bootstrap 95 percent confidence
        interval lies mostly on the wrong side (more than half of its length is below zero).
    Step 3: the hypothesis is
        "supported"      if at least 2 of the 3 fleet sizes PASS;
        "not supported"  if at least 2 of the 3 fleet sizes are WRONG;
        "inconclusive"   in every other case.
    A fleet size that neither passes nor is wrong is called "mixed". "Not supported" is a
    valid and acceptable result. The days used are a previously examined period, not a
    pristine test, so every verdict is a statement about that period only.

Other choices made here
    - The Wilcoxon signed-rank test drops days where both plans earn the same (scipy default,
      zero_method "wilcox"). If all days are tied the p-value is set to 1.
    - The one-sided p-value is for "first plan earns more than second plan".
    - The bootstrap resamples whole days with replacement and takes the median difference
      each time; the interval is the 2.5th and 97.5th percentile. It is seeded.
    - Effect sizes: matched-pairs rank-biserial correlation (1 = first plan always better,
      -1 = always worse) and Cohen's dz (mean difference / standard deviation of differences).
"""

import numpy as np
from scipy import stats

ALPHA = 0.05
MIN_RELATIVE_GAIN_PERCENT = 1.0
MIN_FLEET_SIZES_FOR_VERDICT = 2
BOOTSTRAP_RESAMPLES = 5000
BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
PERCENT = 100.0

STATUS_PASS = "pass"
STATUS_WRONG = "wrong"
STATUS_MIXED = "mixed"
VERDICT_SUPPORTED = "supported"
VERDICT_NOT_SUPPORTED = "not supported"
VERDICT_INCONCLUSIVE = "inconclusive"


def holm_adjust(p_values):
    """Holm step-down correction. Takes raw p-values, returns adjusted p-values in the same order.

    Sort the p-values from smallest to largest; multiply the k-th smallest (k = 1, 2, ...)
    by (m - k + 1) where m is the number of tests; make the list never go down; cap at 1.
    """
    p_values = np.asarray(p_values, dtype=float)
    if p_values.ndim != 1 or len(p_values) == 0:
        raise ValueError("holm_adjust needs a non-empty one-dimensional list of p-values.")
    if np.isnan(p_values).any() or (p_values < 0).any() or (p_values > 1).any():
        raise ValueError("p-values must be numbers between 0 and 1.")
    test_count = len(p_values)
    order = np.argsort(p_values, kind="stable")
    multiplied = p_values[order] * (test_count - np.arange(test_count))
    adjusted_sorted = np.minimum(np.maximum.accumulate(multiplied), 1.0)
    adjusted = np.empty(test_count)
    adjusted[order] = adjusted_sorted
    return adjusted


def make_paired_differences(first_plan_values, second_plan_values):
    """Return first minus second as a float array; stop if the lists differ in length or have NaN."""
    first = np.asarray(first_plan_values, dtype=float)
    second = np.asarray(second_plan_values, dtype=float)
    if first.shape != second.shape or first.ndim != 1:
        raise ValueError(f"Paired lists must have the same length, got {first.shape} and {second.shape}.")
    if len(first) < 2:
        raise ValueError("Need at least 2 paired days.")
    if np.isnan(first).any() or np.isnan(second).any():
        raise ValueError("Paired daily values contain NaN.")
    return first - second


def paired_wilcoxon(differences, alternative="two-sided"):
    """Wilcoxon signed-rank test on paired differences. Returns (statistic, p_value).

    alternative is "two-sided" or "greater" (first plan earns more). All-zero differences
    give p = 1 because the test cannot run without any non-zero difference.
    """
    differences = np.asarray(differences, dtype=float)
    if not np.any(differences != 0):
        return 0.0, 1.0
    result = stats.wilcoxon(differences, alternative=alternative)
    return float(result.statistic), float(result.pvalue)


def rank_biserial_effect(differences):
    """Matched-pairs rank-biserial correlation: (sum of positive ranks - negative ranks) / total."""
    differences = np.asarray(differences, dtype=float)
    non_zero = differences[differences != 0]
    if len(non_zero) == 0:
        return 0.0
    ranks = stats.rankdata(np.abs(non_zero))
    positive_rank_sum = ranks[non_zero > 0].sum()
    negative_rank_sum = ranks[non_zero < 0].sum()
    return float((positive_rank_sum - negative_rank_sum) / ranks.sum())


def cohens_dz(differences):
    """Cohen's dz: mean of the paired differences divided by their standard deviation."""
    differences = np.asarray(differences, dtype=float)
    spread = differences.std(ddof=1)
    if spread == 0:
        return 0.0
    return float(differences.mean() / spread)


def bootstrap_median_ci(differences, seed, resamples=BOOTSTRAP_RESAMPLES,
                        confidence_level=BOOTSTRAP_CONFIDENCE_LEVEL):
    """Seeded bootstrap interval for the median paired difference. Returns (lower, upper)."""
    differences = np.asarray(differences, dtype=float)
    rng = np.random.default_rng(seed)
    day_choices = rng.integers(0, len(differences), size=(resamples, len(differences)))
    resampled_medians = np.median(differences[day_choices], axis=1)
    tail_percent = (1 - confidence_level) / 2 * PERCENT
    lower, upper = np.percentile(resampled_medians, [tail_percent, PERCENT - tail_percent])
    return float(lower), float(upper)


def relative_gain_percent(median_difference, second_plan_median_profit):
    """Median difference as a percent of the second plan's median daily profit per car.

    The size of the second plan's profit is used (absolute value) so a loss-making second
    plan does not flip the sign. NaN when the second plan's median profit is exactly zero.
    """
    if second_plan_median_profit == 0:
        return float("nan")
    return float(PERCENT * median_difference / abs(second_plan_median_profit))


def interval_is_mostly_wrong_side(lower, upper):
    """True when more than half of the interval lies below zero (the wrong direction)."""
    if upper <= 0:
        return True
    if lower >= 0:
        return False
    return (lower + upper) < 0


def classify_fleet_size(p_holm_one_sided, median_difference, relative_gain, ci_lower, ci_upper):
    """Verdict rule step 1 and 2: return "pass", "wrong" or "mixed" for one fleet size."""
    is_gain_large_enough = bool(relative_gain >= MIN_RELATIVE_GAIN_PERCENT)
    if p_holm_one_sided < ALPHA and median_difference > 0 and is_gain_large_enough:
        return STATUS_PASS
    if median_difference <= 0:
        return STATUS_WRONG
    if p_holm_one_sided >= ALPHA and interval_is_mostly_wrong_side(ci_lower, ci_upper):
        return STATUS_WRONG
    return STATUS_MIXED


def decide_verdict(fleet_size_statuses):
    """Verdict rule step 3: combine the status of each fleet size into one verdict."""
    statuses = list(fleet_size_statuses)
    if not statuses:
        raise ValueError("No fleet-size statuses to decide on.")
    if statuses.count(STATUS_PASS) >= MIN_FLEET_SIZES_FOR_VERDICT:
        return VERDICT_SUPPORTED
    if statuses.count(STATUS_WRONG) >= MIN_FLEET_SIZES_FOR_VERDICT:
        return VERDICT_NOT_SUPPORTED
    return VERDICT_INCONCLUSIVE


def compare_two_plans(first_plan_values, second_plan_values, bootstrap_seed):
    """Compare two plans day by day. Returns a dict with every number one table row needs.

    first_plan_values, second_plan_values: daily profit per car (USD) on the same days.
    The Holm-adjusted p-value and the verdict are added later, because they need all the
    hypotheses of the same fleet size.
    """
    differences = make_paired_differences(first_plan_values, second_plan_values)
    second_median = float(np.median(second_plan_values))
    median_difference = float(np.median(differences))
    ci_lower, ci_upper = bootstrap_median_ci(differences, bootstrap_seed)
    statistic, p_two_sided = paired_wilcoxon(differences, "two-sided")
    _, p_one_sided = paired_wilcoxon(differences, "greater")
    return {
        "n_days": len(differences),
        "median_first_plan_usd": float(np.median(first_plan_values)),
        "median_second_plan_usd": second_median,
        "median_difference_usd": median_difference,
        "relative_gain_percent": relative_gain_percent(median_difference, second_median),
        "mean_difference_usd": float(differences.mean()),
        "ci95_lower_usd": ci_lower,
        "ci95_upper_usd": ci_upper,
        "wilcoxon_statistic": statistic,
        "p_two_sided": p_two_sided,
        "p_one_sided": p_one_sided,
        "win_share": float(np.mean(differences > 0)),
        "tie_share": float(np.mean(differences == 0)),
        "rank_biserial": rank_biserial_effect(differences),
        "cohens_dz": cohens_dz(differences),
    }
