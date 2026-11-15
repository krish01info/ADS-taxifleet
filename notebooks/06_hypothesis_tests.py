"""Phase 6 runner: test hypotheses H2-H5 in the fleet simulation and save every result.

Where it fits: Phase 1 gives the demand tables, Phase 3 the forecasts, Phase 4 the fleet
simulator (src/fleet_sim.py) and Phase 5 the launch areas. This script runs the simulator on
many days for many plans and compares the plans with paired tests (src/hypothesis_tests.py).
Run from anywhere:  python notebooks/06_hypothesis_tests.py
Add --validation-only to stop after step 1 (nothing is frozen and no test day is simulated).

Order of work (the order matters, it is what keeps the test honest)
    1. VALIDATION (October 2024 only): check the fleet sizes, tune requests_per_car_hour and
       move_ratio, and choose the data-chosen launch area for each fleet size.
    2. FREEZE: write every choice to data/results/frozen_settings.json. The test days are not
       simulated before this file exists, and the test reads its settings back from the file.
    3. TEST (1 Nov - 31 Dec 2024): every plan runs on the SAME days with the SAME simulated
       requests (one seed per day). Paired Wilcoxon tests, Holm correction, bootstrap
       intervals, sensitivity runs, fairness numbers, figures.

The test months were already looked at by the team, so every number is labelled
"previously examined period, not a pristine test". January 2025 is not used here.

Hypotheses (first plan should earn more than second plan, daily profit per car)
    H2  data-chosen launch area vs the fixed Manhattan + Downtown Brooklyn area (profit plan)
    H3  profit placement vs ride-count placement (same area, same forecast pred_model)
    H4  profit placement with pred_model vs profit placement with copy-last-week
    H5  wait_or_move vs stay
H3-H5 are tested in the fixed baseline area (decided in advance, not tuned). The same tests in
the data-chosen area are reported as a robustness check. Extra comparisons X1-X4 (other
forecasts, random, equal) were planned. P1-P2 (placement vs stay) were added after the first
look at the test results and are labelled post hoc. The minimum service level of the
design is REPORTED (lowest zone share, service gap), it is not enforced by the simulator.
"""

import json
import shutil
import sys
import time
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import fleet_sim
from src import hypothesis_tests as ht

# ------------------------------------------------------------------ files and labels
RESULTS_FOLDER_NAME = "data/results"
REPORT_ASSETS_FOLDER_NAME = "docs/report_assets"
FORECAST_FILE_NAME = "data/forecasts/forecast_zone_hour.parquet"
LAUNCH_AREAS_FILE_NAME = "data/launch_area/launch_areas.csv"
LAUNCH_AREAS_SUMMARY_FILE_NAME = "data/launch_area/launch_areas_summary.csv"
FROZEN_SETTINGS_FILE_NAME = "frozen_settings.json"
MAIN_GRAPH_NAME = "river_links"
BASELINE_LABEL = "fixed_baseline"
CANDIDATE_PREFIX = "proxy_optimal_"
DATA_LABEL = "previously examined period, not a pristine test"

# ------------------------------------------------------------------ days
VALIDATION_FIRST_DAY = date(2024, 10, 1)
VALIDATION_LAST_DAY = date(2024, 10, 31)
VALIDATION_PERIOD_LABEL = "validation"
TEST_PERIOD_LABEL = "test"
EXPECTED_TEST_HOLIDAYS = (date(2024, 11, 11), date(2024, 11, 28), date(2024, 12, 25))
DST_FALL_BACK_DAY = date(2024, 11, 3)
VALIDATION_ONLY_FLAG = "--validation-only"
FIRST_WEEKEND_WEEKDAY_NUMBER = 5
MAIN_GROUP = "main"
HOLIDAY_GROUP = "holiday"
DST_GROUP = "dst_fall_back"
MIN_DAYS_FOR_TESTS = 6
HOURS_PER_DAY = 24

# ------------------------------------------------------------------ simulation choices
# Fleet-size sets tried in this order. The first (suggested by a teammate) is kept only if every
# size keeps the cars busy between MIN_UTILISATION and MAX_UTILISATION of their time on
# October validation days. At the 1% demand scale the baseline area has only about 2,500
# requests a day, so 250 and 500 cars are mostly idle; the fallback set is smaller.
FLEET_SIZE_SETS = ((100, 250, 500), (25, 50, 100))
SIMULATION_SEED = 20261115
BOOTSTRAP_SEED = 20261116
MIN_UTILISATION = 0.15
MAX_UTILISATION = 0.95
REQUESTS_PER_CAR_HOUR_GRID = (1.0, 2.0, 3.0, 4.0)
MOVE_RATIO_GRID = (0.1, 0.25, 0.5, 0.75, 1.0)
NOMINAL_PRICE = {"base_charge_usd": 2.50, "per_mile_rate_usd": 1.60}
MIN_ZONE_REQUESTS_FOR_SHARE = 30

FORECAST_COLUMNS = ("pred_model", "pred_last_week", "pred_four_week_mean", "pred_frozen_profile")
FALLBACK_FORECAST_COLUMN = "pred_frozen_profile"

# plan name -> (simulator policy, forecast column the policy may look at)
PLANS = {
    "profit_model": ("profit", "pred_model"),
    "ride_count_model": ("ride_count", "pred_model"),
    "profit_last_week": ("profit", "pred_last_week"),
    "profit_four_week_mean": ("profit", "pred_four_week_mean"),
    "profit_frozen_profile": ("profit", "pred_frozen_profile"),
    "random_allocation": ("random", "pred_model"),
    "equal_allocation": ("equal", "pred_model"),
    "stay_model": ("stay", "pred_model"),
    "wait_or_move_model": ("wait_or_move", "pred_model"),
}
SENSITIVITY_PLANS = (
    "profit_model", "ride_count_model", "profit_last_week", "stay_model", "wait_or_move_model",
)
MAIN_SCENARIO = "main"
# scenario name -> (price rule, simulator setting overrides)
SCENARIOS = {
    MAIN_SCENARIO: ("calibrated", {}),
    "cost_per_mile_0.74": ("calibrated", {"cost_per_mile_usd": 0.74}),
    "cost_per_mile_2.00": ("calibrated", {"cost_per_mile_usd": 2.00}),
    "idle_cost_0": ("calibrated", {"idle_cost_per_car_hour_usd": 0.0}),
    "idle_cost_5": ("calibrated", {"idle_cost_per_car_hour_usd": 5.0}),
    "nominal_price_2.50_plus_1.60_per_mile": ("nominal", {}),
}

# (code, description, (area role, plan) of the first plan, (area role, plan) of the second plan)
# area role "area" means: the area of the block being tested (baseline or data-chosen).
PRIMARY_COMPARISONS = (
    ("H2", "data-chosen area vs fixed baseline area (profit plan)",
     ("chosen", "profit_model"), ("baseline", "profit_model")),
    ("H3", "profit placement vs ride-count placement",
     ("area", "profit_model"), ("area", "ride_count_model")),
    ("H4", "forecast model vs copy last week (profit placement)",
     ("area", "profit_model"), ("area", "profit_last_week")),
    ("H5", "wait_or_move vs stay",
     ("area", "wait_or_move_model"), ("area", "stay_model")),
)
EXTRA_COMPARISONS = (
    ("X1", "profit model forecast vs four-week mean forecast",
     ("area", "profit_model"), ("area", "profit_four_week_mean")),
    ("X2", "profit model forecast vs frozen profile forecast",
     ("area", "profit_model"), ("area", "profit_frozen_profile")),
    ("X3", "profit placement vs random allocation",
     ("area", "profit_model"), ("area", "random_allocation")),
    ("X4", "profit placement vs equal allocation",
     ("area", "profit_model"), ("area", "equal_allocation")),
)
# Added AFTER the main results showed that "stay" earned more than the placement plans.
# Exploratory, never part of a verdict about H2-H5; the main hypotheses and extras are unchanged.
POSTHOC_COMPARISONS = (
    ("P1", "profit placement vs stay (post hoc, exploratory)",
     ("area", "profit_model"), ("area", "stay_model")),
    ("P2", "ride-count placement vs stay (post hoc, exploratory)",
     ("area", "ride_count_model"), ("area", "stay_model")),
)
FAMILY_POSTHOC = "posthoc_baseline_area"
FAMILY_PRIMARY = "primary_baseline_area"
FAMILY_ROBUSTNESS = "robustness_chosen_area"
FAMILY_EXTRA = "extra_baseline_area"

# ------------------------------------------------------------------ figures
FIGURE_DPI = 140
SURFACE_COLOUR = "#fcfcfb"
INK_COLOUR = "#0b0b0b"
MUTED_INK_COLOUR = "#52514e"
GRID_COLOUR = "#e1e0d9"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
NEUTRAL_GREY = "#c3c2b7"
FLEET_COLOURS = (BLUE, ORANGE, AQUA)
JITTER_WIDTH = 0.12
FIGURE_FILE_NAMES = {
    "differences": "fig_h_paired_differences.png",
    "plans": "fig_h_profit_per_car_by_plan.png",
    "fairness": "fig_h_service_fairness.png",
    "sensitivity": "fig_h_sensitivity.png",
    "validation": "fig_h_validation_area_choice.png",
}


# ================================================================== forecasts

def read_forecast_table(repo_root):
    """Read the forecast file with duckdb (fresh every run) and add a plain `day` column."""
    path = Path(repo_root) / FORECAST_FILE_NAME
    columns = ", ".join(("zone_id", "event_date_nyc", "hour_nyc", "request_count") + FORECAST_COLUMNS)
    sql = f"select {columns} from read_parquet('{path.as_posix()}')"
    table = duckdb.connect().execute(sql).df()
    table["day"] = pd.to_datetime(table["event_date_nyc"]).dt.date
    for column in ("request_count",) + FORECAST_COLUMNS:
        table[column] = table[column].astype("float64")
    return table


def build_day_arrays(table, zone_ids, column):
    """One (24, zones) array per civil day for one column; NaN where the file has no value.

    The clock-change day has two rows for hour 1; they are averaged (the forecasts are equal).
    """
    averaged = table.groupby(["day", "hour_nyc", "zone_id"], as_index=False)[column].mean()
    days = sorted(averaged["day"].unique())
    day_position = {day: position for position, day in enumerate(days)}
    zone_position = {int(zone_id): position for position, zone_id in enumerate(zone_ids)}
    cube = np.full((len(days), HOURS_PER_DAY, len(zone_ids)), np.nan)
    day_index = averaged["day"].map(day_position).to_numpy()
    zone_index = averaged["zone_id"].map(zone_position).to_numpy()
    cube[day_index, averaged["hour_nyc"].to_numpy(), zone_index] = averaged[column].to_numpy()
    return {day: cube[position] for day, position in day_position.items()}


def build_forecasts(table, zone_ids):
    """Build the four forecast sources as {column: {day: (24, zones) array}}, with no gaps.

    A missing forecast value (for example copy-last-week for the hour after a clock change)
    is replaced by the frozen-profile forecast of the same zone-hour. Returns the forecasts
    and a list that says how many cells were replaced on which days.
    """
    if set(table["zone_id"].unique()) != {int(zone_id) for zone_id in zone_ids}:
        raise ValueError("The forecast file and the simulator do not have the same zones.")
    arrays = {column: build_day_arrays(table, zone_ids, column) for column in FORECAST_COLUMNS}
    fallback = arrays[FALLBACK_FORECAST_COLUMN]
    fill_report = []
    for column in FORECAST_COLUMNS:
        for day, array in arrays[column].items():
            missing = np.isnan(array)
            if not missing.any():
                continue
            if column == FALLBACK_FORECAST_COLUMN:
                raise ValueError(f"{FALLBACK_FORECAST_COLUMN} has missing values on {day}.")
            array[missing] = fallback[day][missing]
            fill_report.append({"column": column, "day": day.isoformat(),
                                "cells_replaced": int(missing.sum()),
                                "hours_affected": sorted(int(hour) for hour in np.flatnonzero(missing.any(axis=1)))})
    return arrays, fill_report


def check_actuals_match_simulator(table, inputs, days):
    """Stop if the forecast file's actual request counts differ from the simulator's inputs."""
    actual_arrays = build_day_arrays(table, inputs["zone_ids"], "request_count")
    for day in days:
        same = np.allclose(actual_arrays[day], inputs["hourly_requests"][day], equal_nan=True)
        if not same:
            raise ValueError(f"Actual requests in the forecast file differ from the simulator on {day}.")
    print(f"Actual request counts in the forecast file equal the simulator inputs on {len(days)} days.")


def check_forecasts_cover_days(forecasts, days):
    """Stop if a forecast source lacks one of the days we are about to simulate."""
    for column, by_day in forecasts.items():
        missing_days = [day for day in days if day not in by_day]
        if missing_days:
            raise ValueError(f"{column} has no forecast for {missing_days[:3]} ...")


def hash_file(path):
    """SHA-256 of a file, so the frozen settings say exactly which forecast file was used."""
    return sha256(Path(path).read_bytes()).hexdigest()


# ================================================================== days and areas

def pick_days_in_period(inputs, period_label, first_day=None, last_day=None):
    """Sorted list of civil days with a period label, optionally cut to a date range."""
    day_table = inputs["day_table"]
    days = day_table.loc[day_table["period_label"] == period_label, "date"]
    return sorted(day for day in days
                  if (first_day is None or day >= first_day) and (last_day is None or day <= last_day))


def find_holiday_days(inputs, days):
    """The federal holidays among days, taken from the release calendar."""
    day_table = inputs["day_table"].set_index("date")
    return sorted(day for day in days if bool(day_table.loc[day, "is_federal_holiday"]))


def label_day_group(day, holiday_days):
    """main, holiday or dst_fall_back. Holidays and the clock-change day stay out of the main test."""
    if day in holiday_days:
        return HOLIDAY_GROUP
    if day == DST_FALL_BACK_DAY:
        return DST_GROUP
    return MAIN_GROUP


def is_weekend_day(day):
    """True for Saturday and Sunday."""
    return day.weekday() >= FIRST_WEEKEND_WEEKDAY_NUMBER


def load_candidate_areas(repo_root):
    """Read launch_areas.csv (river_links graph) into {label: sorted zone id list}.

    Checks the zone counts against launch_areas_summary.csv for the same graph version.
    """
    table = pd.read_csv(Path(repo_root) / LAUNCH_AREAS_FILE_NAME)
    areas = {label: sorted(int(zone_id) for zone_id in group["zone_id"])
             for label, group in table.groupby("area_label")}
    summary = pd.read_csv(Path(repo_root) / LAUNCH_AREAS_SUMMARY_FILE_NAME)
    summary = summary[summary["graph"] == MAIN_GRAPH_NAME].set_index("area_label")["zone_count"]
    for label, zone_ids in areas.items():
        if int(summary[label]) != len(zone_ids):
            raise ValueError(f"Area {label} has {len(zone_ids)} zones, the summary says {summary[label]}.")
    if BASELINE_LABEL not in areas:
        raise ValueError(f"{BASELINE_LABEL} is missing from {LAUNCH_AREAS_FILE_NAME}.")
    candidate_labels = sorted((label for label in areas if label.startswith(CANDIDATE_PREFIX)),
                              key=lambda label: len(areas[label]))
    if not candidate_labels:
        raise ValueError("No proxy-optimal candidate areas found.")
    return areas, candidate_labels


# ================================================================== simulation

def make_scenario_settings(price_options, scenario, n_cars_settings):
    """Simulator settings for one scenario and one fleet size.

    price_options: {"calibrated": {...}, "nominal": {...}}. n_cars_settings: the frozen
    requests_per_car_hour and move_ratio of that fleet size.
    """
    price_rule, overrides = SCENARIOS[scenario]
    price = price_options[price_rule]
    return fleet_sim.make_settings(
        price["base_charge_usd"], price["per_mile_rate_usd"],
        requests_per_car_hour=n_cars_settings["requests_per_car_hour"],
        move_ratio=n_cars_settings["move_ratio"], **overrides,
    )


def generate_all_requests(inputs, days, settings):
    """Simulated requests for each day, drawn once and shared by every plan, area and fleet size."""
    return {day: fleet_sim.generate_day_requests(inputs, day, settings, SIMULATION_SEED) for day in days}


def run_plans(inputs, requests_by_day, forecasts, day_groups, plan_names, n_cars, area_label,
              zone_ids, settings, scenario, period, zone_totals=None):
    """Simulate every plan on every day in day_groups. Returns a list of result rows (dicts).

    If zone_totals (a dict) is given, requests and served trips per zone are added up over
    the main-group days for each plan (used for the fairness numbers).
    """
    rows = []
    for plan in plan_names:
        policy, forecast_column = PLANS[plan]
        for day, day_group in day_groups.items():
            summary, zone_table = fleet_sim.simulate_day(
                inputs, requests_by_day[day], forecasts[forecast_column][day], policy, n_cars,
                zone_ids, settings, SIMULATION_SEED)
            identifiers = {"period": period, "scenario": scenario, "day_group": day_group,
                           "is_weekend": is_weekend_day(day), "area_label": area_label, "plan": plan}
            summary["utilisation"] = 1 - summary["idle_car_hours"] / (n_cars * HOURS_PER_DAY)
            rows.append({**identifiers, **summary})
            if zone_totals is not None and day_group == MAIN_GROUP:
                add_zone_totals(zone_totals, plan, zone_table)
    return rows


def add_zone_totals(zone_totals, plan, zone_table):
    """Add one day's requests and served trips per zone to the running totals of a plan."""
    counts = zone_table[["zone_id", "requests", "served"]]
    if plan not in zone_totals:
        zone_totals[plan] = counts.copy()
    else:
        zone_totals[plan][["requests", "served"]] += counts[["requests", "served"]]


# ================================================================== validation (October)

def summarise_validation(rows, step, candidate_name):
    """Mean results of a list of validation rows, as one dict for the tuning table."""
    table = pd.DataFrame(rows)
    return {"step": step, "n_cars": int(table["n_cars"].iloc[0]), "candidate": candidate_name,
            "validation_days": table["date"].nunique(),
            "mean_profit_per_car_usd": float(table["profit_per_car_usd"].mean()),
            "mean_utilisation": float(table["utilisation"].mean()),
            "mean_served_share": float(table["served_share"].mean()),
            "mean_empty_mile_share": float(table["empty_mile_share"].mean())}


def pick_best_candidate(tuning_rows, step, n_cars):
    """Name of the candidate with the highest mean validation profit per car (first one wins ties)."""
    rows = [row for row in tuning_rows if row["step"] == step and row["n_cars"] == n_cars]
    return max(rows, key=lambda row: row["mean_profit_per_car_usd"])["candidate"]


def run_validation_choices(inputs, requests_by_day, forecasts, validation_days, areas,
                           candidate_labels, price_options):
    """Check fleet sizes, tune two settings and choose the launch area, on October days only.

    Returns (fleet sizes, frozen choices per fleet size, tuning table rows).
    """
    day_groups = {day: MAIN_GROUP for day in validation_days}
    tuning_rows = []

    def run_validation(plan, n_cars, area_label, rph, move_ratio):
        settings = make_scenario_settings(price_options, MAIN_SCENARIO,
                                          {"requests_per_car_hour": rph, "move_ratio": move_ratio})
        return run_plans(inputs, requests_by_day, forecasts, day_groups, [plan], n_cars,
                         area_label, areas[area_label], settings, "validation", VALIDATION_PERIOD_LABEL)

    default_rph = fleet_sim.REQUESTS_PER_CAR_HOUR
    default_ratio = fleet_sim.MOVE_RATIO
    fleet_sizes, fleet_check_rows = choose_fleet_sizes(run_validation, default_rph, default_ratio)
    tuning_rows += fleet_check_rows
    choices = {}
    for n_cars in fleet_sizes:
        for rph in REQUESTS_PER_CAR_HOUR_GRID:
            rows = run_validation("profit_model", n_cars, BASELINE_LABEL, rph, default_ratio)
            tuning_rows.append(summarise_validation(rows, "requests_per_car_hour", rph))
        best_rph = pick_best_candidate(tuning_rows, "requests_per_car_hour", n_cars)
        for ratio in MOVE_RATIO_GRID:
            rows = run_validation("wait_or_move_model", n_cars, BASELINE_LABEL, best_rph, ratio)
            tuning_rows.append(summarise_validation(rows, "move_ratio", ratio))
        best_ratio = pick_best_candidate(tuning_rows, "move_ratio", n_cars)
        for area_label in [BASELINE_LABEL] + candidate_labels:
            rows = run_validation("profit_model", n_cars, area_label, best_rph, best_ratio)
            tuning_rows.append(summarise_validation(rows, "launch_area", area_label))
        candidate_rows = [row for row in tuning_rows if row["step"] == "launch_area"
                          and row["n_cars"] == n_cars and row["candidate"] in candidate_labels]
        chosen_label = max(candidate_rows, key=lambda row: row["mean_profit_per_car_usd"])["candidate"]
        choices[n_cars] = {"requests_per_car_hour": float(best_rph), "move_ratio": float(best_ratio),
                           "chosen_area_label": chosen_label,
                           "chosen_area_zone_ids": areas[chosen_label]}
    return fleet_sizes, choices, tuning_rows


def choose_fleet_sizes(run_validation, default_rph, default_ratio):
    """Pick the first fleet-size set whose every size has validation utilisation inside the band.

    Utilisation = share of car-hours that cars are busy (profit plan, baseline area, default
    settings, October days). The suggested set is tried first, then the fallback set.
    Returns (fleet sizes, one summary row per size that was checked).
    """
    checked = {}
    for size_set in FLEET_SIZE_SETS:
        for n_cars in size_set:
            if n_cars not in checked:
                rows = run_validation("profit_model", n_cars, BASELINE_LABEL, default_rph, default_ratio)
                checked[n_cars] = summarise_validation(rows, "fleet_size_check", "default settings")
        utilisations = [checked[n_cars]["mean_utilisation"] for n_cars in size_set]
        if all(MIN_UTILISATION <= value <= MAX_UTILISATION for value in utilisations):
            return list(size_set), list(checked.values())
        print(f"Fleet sizes {size_set} rejected: validation utilisation "
              f"{[round(value, 3) for value in utilisations]} is outside "
              f"[{MIN_UTILISATION}, {MAX_UTILISATION}].")
    raise ValueError("No fleet-size set has validation utilisation inside the allowed band.")


def save_frozen_settings(results_folder, fleet_sizes, choices, areas, price_fit, validation_days, test_days,
                         holiday_days, forecast_path, forecast_row_count, fill_report):
    """Write the frozen settings file. Everything the test needs is in this file."""
    frozen = {
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "data_label": DATA_LABEL,
        "test_days_simulated_before_this_file_was_written": False,
        "validation_days": [day.isoformat() for day in validation_days],
        "test_days_main_group": [day.isoformat() for day in test_days if label_day_group(day, holiday_days) == MAIN_GROUP],
        "test_days_holidays_reported_separately": [day.isoformat() for day in holiday_days],
        "test_day_clock_change_reported_separately": DST_FALL_BACK_DAY.isoformat(),
        "fleet_sizes": [int(n_cars) for n_cars in fleet_sizes],
        "fleet_size_sets_tried_in_order": [list(size_set) for size_set in FLEET_SIZE_SETS],
        "utilisation_band": [MIN_UTILISATION, MAX_UTILISATION],
        "demand_scale": fleet_sim.DEMAND_SCALE,
        "simulation_seed": SIMULATION_SEED,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_resamples": ht.BOOTSTRAP_RESAMPLES,
        "price_calibrated": price_fit,
        "price_nominal": NOMINAL_PRICE,
        "settings_by_fleet_size": {str(n_cars): choice for n_cars, choice in choices.items()},
        "baseline_area_zone_ids": areas[BASELINE_LABEL],
        "forecast_file": {"path": FORECAST_FILE_NAME, "sha256": hash_file(forecast_path),
                          "rows": int(forecast_row_count)},
        "forecast_gaps_filled_with_frozen_profile": fill_report,
        "choice_rule": ("Highest mean validation profit per car (October 2024, holiday days left out) "
                        "for requests_per_car_hour (profit plan) and move_ratio (wait_or_move), then "
                        "the proxy-optimal area with the highest mean profit per car. First candidate "
                        "wins ties. Nothing was tuned on test days."),
    }
    path = Path(results_folder) / FROZEN_SETTINGS_FILE_NAME
    path.write_text(json.dumps(frozen, indent=2))
    return path


# ================================================================== test period

def run_test_period(inputs, forecasts, frozen, areas, test_days, holiday_days):
    """Simulate every plan on every test day. Returns (daily results table, zone totals).

    Settings come only from the frozen file. Main scenario: every plan, in the baseline area
    and in each fleet size's data-chosen area. Sensitivity scenarios: the five plans needed
    for H2-H5.
    """
    day_groups = {day: label_day_group(day, holiday_days) for day in test_days}
    price_options = {"calibrated": frozen["price_calibrated"], "nominal": frozen["price_nominal"]}
    requests_by_day = generate_all_requests(inputs, test_days, fleet_sim.make_settings(
        0.0, 0.0, demand_scale=frozen["demand_scale"]))
    rows = []
    zone_totals = {}
    for scenario in SCENARIOS:
        for n_cars in frozen["fleet_sizes"]:
            fleet_settings = frozen["settings_by_fleet_size"][str(n_cars)]
            settings = make_scenario_settings(price_options, scenario, fleet_settings)
            chosen_label = fleet_settings["chosen_area_label"]
            area_ids = {BASELINE_LABEL: frozen["baseline_area_zone_ids"],
                        chosen_label: fleet_settings["chosen_area_zone_ids"]}
            for area_label, zone_ids in area_ids.items():
                is_main = scenario == MAIN_SCENARIO
                if is_main:
                    plan_names = list(PLANS)
                elif area_label == BASELINE_LABEL:
                    plan_names = list(SENSITIVITY_PLANS)
                else:
                    plan_names = ["profit_model"]
                totals = {} if is_main else None
                rows += run_plans(inputs, requests_by_day, forecasts, day_groups, plan_names, n_cars,
                                  area_label, zone_ids, settings, scenario, TEST_PERIOD_LABEL, totals)
                if is_main:
                    zone_totals[(n_cars, area_label)] = totals
        print(f"  scenario {scenario}: done ({len(rows)} rows so far)")
    return pd.DataFrame(rows), zone_totals


# ================================================================== statistics

def get_plan_series(daily, scenario, n_cars, area_label, plan, days):
    """Daily profit per car (numpy array) of one plan on the given days, in day order."""
    selected = daily[(daily["scenario"] == scenario) & (daily["n_cars"] == n_cars)
                     & (daily["area_label"] == area_label) & (daily["plan"] == plan)]
    series = selected.set_index("date")["profit_per_car_usd"]
    if not series.index.is_unique:
        raise ValueError(f"Duplicate days for {scenario}, {n_cars}, {area_label}, {plan}.")
    values = series.reindex(days)
    if values.isna().any():
        raise ValueError(f"Missing days for {scenario}, {n_cars}, {area_label}, {plan}.")
    return values.to_numpy()


def resolve_area_label(role, area_role, fleet_settings):
    """Turn "baseline", "chosen" or "area" into an area label for one fleet size."""
    resolved_role = area_role if role == "area" else role
    return BASELINE_LABEL if resolved_role == "baseline" else fleet_settings["chosen_area_label"]


def get_comparison_values(daily, frozen, scenario, n_cars, comparison, area_role, days):
    """Return (first plan values, second plan values) for one comparison, fleet size and day list."""
    _, _, (first_role, first_plan), (second_role, second_plan) = comparison
    fleet_settings = frozen["settings_by_fleet_size"][str(n_cars)]
    first = get_plan_series(daily, scenario, n_cars, resolve_area_label(first_role, area_role, fleet_settings),
                            first_plan, days)
    second = get_plan_series(daily, scenario, n_cars, resolve_area_label(second_role, area_role, fleet_settings),
                             second_plan, days)
    return first, second


def describe_without_tests(first, second):
    """Simple numbers for a group with too few days for a test (holidays, the clock-change day)."""
    differences = first - second
    median_difference = float(np.median(differences))
    nan = float("nan")
    return {"n_days": len(differences), "median_first_plan_usd": float(np.median(first)),
            "median_second_plan_usd": float(np.median(second)),
            "median_difference_usd": median_difference,
            "relative_gain_percent": ht.relative_gain_percent(median_difference, float(np.median(second))),
            "mean_difference_usd": float(differences.mean()), "ci95_lower_usd": nan, "ci95_upper_usd": nan,
            "wilcoxon_statistic": nan, "p_two_sided": nan, "p_one_sided": nan,
            "win_share": float(np.mean(differences > 0)), "tie_share": float(np.mean(differences == 0)),
            "rank_biserial": nan, "cohens_dz": nan}


def compare_family(daily, frozen, scenario, days, comparisons, area_role, family, day_subset):
    """Run the paired tests of one family of comparisons for every fleet size.

    Holm correction is applied over the comparisons of the family at each fleet size.
    The verdict of each comparison combines its three fleet sizes (see src/hypothesis_tests.py).
    Groups with fewer than MIN_DAYS_FOR_TESTS days only get descriptive numbers.
    """
    rows = []
    can_test = len(days) >= MIN_DAYS_FOR_TESTS
    for n_cars in frozen["fleet_sizes"]:
        fleet_rows = []
        for comparison_number, comparison in enumerate(comparisons):
            code, description, _, _ = comparison
            first, second = get_comparison_values(daily, frozen, scenario, n_cars, comparison, area_role, days)
            if can_test:
                stats_row = ht.compare_two_plans(first, second, [BOOTSTRAP_SEED, n_cars, comparison_number])
            else:
                stats_row = describe_without_tests(first, second)
            fleet_rows.append({"family": family, "scenario": scenario, "day_subset": day_subset,
                               "hypothesis": code, "description": description, "area_role": area_role,
                               "n_cars": n_cars, **stats_row})
        if can_test:
            holm = ht.holm_adjust([row["p_one_sided"] for row in fleet_rows])
            for row, adjusted in zip(fleet_rows, holm):
                row["p_holm_one_sided"] = float(adjusted)
        rows += fleet_rows
    table = pd.DataFrame(rows)
    if not can_test:
        table["fleet_size_status"] = "not tested (too few days)"
        table["verdict"] = "not tested (too few days)"
        return table
    table["p_holm_all_fleet_sizes"] = ht.holm_adjust(table["p_one_sided"].to_numpy())
    table["fleet_size_status"] = [
        ht.classify_fleet_size(row.p_holm_one_sided, row.median_difference_usd, row.relative_gain_percent,
                               row.ci95_lower_usd, row.ci95_upper_usd) for row in table.itertuples()]
    verdicts = {code: ht.decide_verdict(group["fleet_size_status"])
                for code, group in table.groupby("hypothesis")}
    table["verdict"] = table["hypothesis"].map(verdicts)
    return table


def build_all_comparison_tables(daily, frozen, main_days, weekday_days, weekend_days, holiday_days):
    """All comparison tables: main hypotheses, robustness, extras, subgroups, sensitivity."""
    hypothesis_tables = [
        compare_family(daily, frozen, MAIN_SCENARIO, main_days, PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "main_days"),
        compare_family(daily, frozen, MAIN_SCENARIO, main_days, PRIMARY_COMPARISONS, "chosen",
                       FAMILY_ROBUSTNESS, "main_days"),
        compare_family(daily, frozen, MAIN_SCENARIO, main_days, EXTRA_COMPARISONS, "baseline",
                       FAMILY_EXTRA, "main_days"),
        compare_family(daily, frozen, MAIN_SCENARIO, main_days, POSTHOC_COMPARISONS, "baseline",
                       FAMILY_POSTHOC, "main_days"),
    ]
    hypothesis_summary = pd.concat(hypothesis_tables, ignore_index=True)
    hypothesis_summary = hypothesis_summary[~((hypothesis_summary["family"] == FAMILY_ROBUSTNESS)
                                              & (hypothesis_summary["hypothesis"] == "H2"))]
    subgroup_tables = [
        compare_family(daily, frozen, MAIN_SCENARIO, weekday_days, PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "weekdays"),
        compare_family(daily, frozen, MAIN_SCENARIO, weekend_days, PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "weekends"),
        compare_family(daily, frozen, MAIN_SCENARIO, holiday_days, PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "federal_holidays"),
        compare_family(daily, frozen, MAIN_SCENARIO, [DST_FALL_BACK_DAY], PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "clock_change_day"),
    ]
    sensitivity_tables = [
        compare_family(daily, frozen, scenario, main_days, PRIMARY_COMPARISONS, "baseline",
                       FAMILY_PRIMARY, "main_days") for scenario in SCENARIOS
    ]
    return (hypothesis_summary, pd.concat(subgroup_tables, ignore_index=True),
            pd.concat(sensitivity_tables, ignore_index=True))


# ================================================================== summaries

def build_plan_summary(daily, main_days):
    """Median, mean and spread of daily profit per car per plan, area and fleet size (main scenario)."""
    main = daily[(daily["scenario"] == MAIN_SCENARIO) & (daily["date"].isin(main_days))]
    grouped = main.groupby(["n_cars", "area_label", "plan"])
    summary = grouped.agg(
        n_days=("date", "nunique"),
        median_profit_per_car_usd=("profit_per_car_usd", "median"),
        mean_profit_per_car_usd=("profit_per_car_usd", "mean"),
        q25_profit_per_car_usd=("profit_per_car_usd", lambda values: values.quantile(0.25)),
        q75_profit_per_car_usd=("profit_per_car_usd", lambda values: values.quantile(0.75)),
        mean_utilisation=("utilisation", "mean"),
        mean_served_share=("served_share", "mean"),
        mean_empty_mile_share=("empty_mile_share", "mean"),
        mean_moved_cars=("moved_cars", "mean"),
    ).reset_index()
    return summary


def build_fairness_summary(daily, main_days, zone_totals):
    """Service gap and lowest zone share per plan, area and fleet size (main scenario, main days).

    The minimum service level is REPORTED here, it is not enforced inside the simulator.
    lowest_zone_share_over_period uses only zones with at least MIN_ZONE_REQUESTS_FOR_SHARE
    simulated requests over all main days, because one-day zone shares at a 1% demand scale
    are based on a handful of requests.
    """
    main = daily[(daily["scenario"] == MAIN_SCENARIO) & (daily["date"].isin(main_days))]
    grouped = main.groupby(["n_cars", "area_label", "plan"]).agg(
        mean_served_share=("served_share", "mean"),
        mean_service_gap=("service_gap", "mean"),
        median_service_gap=("service_gap", "median"),
        mean_lowest_zone_share_per_day=("lowest_zone_served_share", "mean"),
        mean_share_high_demand_zones=("served_share_high_demand_zones", "mean"),
        mean_share_low_demand_zones=("served_share_low_demand_zones", "mean"),
    ).reset_index()
    lowest_shares, zones_counted, zones_never_served = [], [], []
    for row in grouped.itertuples():
        totals = zone_totals[(row.n_cars, row.area_label)][row.plan]
        enough = totals[totals["requests"] >= MIN_ZONE_REQUESTS_FOR_SHARE]
        shares = enough["served"] / enough["requests"]
        lowest_shares.append(float(shares.min()))
        zones_counted.append(len(enough))
        zones_never_served.append(int((enough["served"] == 0).sum()))
    grouped["lowest_zone_share_over_period"] = lowest_shares
    grouped["zones_counted_for_lowest_share"] = zones_counted
    grouped["zones_with_no_served_trip"] = zones_never_served
    grouped["service_level_note"] = "minimum service level is reported, not enforced"
    return grouped


# ================================================================== figures

def style_axes(axes):
    """Light, quiet axes: no top/right frame, hairline grid."""
    axes.set_facecolor(SURFACE_COLOUR)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(NEUTRAL_GREY)
    axes.tick_params(colors=MUTED_INK_COLOUR, labelsize=8)
    axes.grid(True, color=GRID_COLOUR, linewidth=0.6)
    axes.set_axisbelow(True)


def finish_figure(figure, results_folder, report_assets_folder, key):
    """Save a figure to the results folder and copy it to docs/report_assets."""
    figure.patch.set_facecolor(SURFACE_COLOUR)
    figure.tight_layout()
    path = Path(results_folder) / FIGURE_FILE_NAMES[key]
    figure.savefig(path, dpi=FIGURE_DPI, facecolor=SURFACE_COLOUR)
    plt.close(figure)
    Path(report_assets_folder).mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, Path(report_assets_folder) / FIGURE_FILE_NAMES[key])
    return path


def draw_paired_differences_figure(daily, frozen, main_days, summary, folders):
    """One panel per hypothesis: every day's paired difference for each fleet size."""
    primary = summary[summary["family"] == FAMILY_PRIMARY]
    figure, all_axes = plt.subplots(1, len(PRIMARY_COMPARISONS), figsize=(15, 4.6), sharey=False)
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    for axes, comparison in zip(all_axes, PRIMARY_COMPARISONS):
        code, description, _, _ = comparison
        for position, n_cars in enumerate(frozen["fleet_sizes"]):
            first, second = get_comparison_values(daily, frozen, MAIN_SCENARIO, n_cars, comparison,
                                                  "baseline", main_days)
            differences = first - second
            colour = FLEET_COLOURS[position]
            axes.boxplot(differences, positions=[position], widths=0.5, showfliers=False,
                         medianprops={"color": INK_COLOUR, "linewidth": 1.6},
                         boxprops={"color": colour}, whiskerprops={"color": colour},
                         capprops={"color": colour})
            jitter = rng.uniform(-JITTER_WIDTH, JITTER_WIDTH, len(differences))
            axes.scatter(position + jitter, differences, s=9, color=colour, alpha=0.6, zorder=3)
        axes.axhline(0, color=MUTED_INK_COLOUR, linewidth=1.0)
        verdict = primary.loc[primary["hypothesis"] == code, "verdict"].iloc[0]
        axes.set_title(f"{code}: {description}\nverdict: {verdict}", fontsize=8.5, color=INK_COLOUR)
        axes.set_xticks(range(len(frozen["fleet_sizes"])))
        axes.set_xticklabels([f"{n} cars" for n in frozen["fleet_sizes"]])
        axes.set_ylabel("first minus second plan, USD per car per day", fontsize=8, color=MUTED_INK_COLOUR)
        style_axes(axes)
    figure.suptitle(f"Paired daily profit differences, main test days ({DATA_LABEL})",
                    fontsize=10, color=INK_COLOUR)
    return finish_figure(figure, *folders, "differences")


def draw_profit_by_plan_figure(plan_summary, frozen, folders):
    """Median daily profit per car for each plan, one panel per fleet size (baseline area)."""
    plan_order = list(PLANS)[::-1]
    figure, all_axes = plt.subplots(1, len(frozen["fleet_sizes"]), figsize=(15, 4.8))
    for axes, n_cars in zip(all_axes, frozen["fleet_sizes"]):
        rows = plan_summary[(plan_summary["n_cars"] == n_cars) & (plan_summary["area_label"] == BASELINE_LABEL)]
        rows = rows.set_index("plan").loc[plan_order]
        medians = rows["median_profit_per_car_usd"].to_numpy()
        lower = medians - rows["q25_profit_per_car_usd"].to_numpy()
        upper = rows["q75_profit_per_car_usd"].to_numpy() - medians
        axes.barh(range(len(plan_order)), medians, color=BLUE, height=0.6,
                  xerr=[lower, upper], error_kw={"ecolor": MUTED_INK_COLOUR, "elinewidth": 1})
        axes.set_yticks(range(len(plan_order)))
        axes.set_yticklabels(plan_order, fontsize=8)
        axes.set_title(f"{n_cars} cars", fontsize=10, color=INK_COLOUR)
        axes.set_xlabel("median daily profit per car, USD (bars: 25th-75th percentile)",
                        fontsize=8, color=MUTED_INK_COLOUR)
        style_axes(axes)
    figure.suptitle(f"Profit per car by plan, fixed baseline area, main test days ({DATA_LABEL})",
                    fontsize=10, color=INK_COLOUR)
    return finish_figure(figure, *folders, "plans")


def draw_fairness_figure(fairness, frozen, folders):
    """Service gap (top) and lowest zone share (bottom) by plan, one column per fleet size."""
    plan_order = list(PLANS)[::-1]
    fleet_count = len(frozen["fleet_sizes"])
    figure, all_axes = plt.subplots(2, fleet_count, figsize=(15, 8.5))
    measures = (("mean_service_gap", "mean service gap (high minus low demand zones)"),
                ("lowest_zone_share_over_period", "lowest zone served share over the period"))
    for row_number, (column, label) in enumerate(measures):
        for axes, n_cars in zip(all_axes[row_number], frozen["fleet_sizes"]):
            rows = fairness[(fairness["n_cars"] == n_cars) & (fairness["area_label"] == BASELINE_LABEL)]
            rows = rows.set_index("plan").loc[plan_order]
            axes.barh(range(len(plan_order)), rows[column].to_numpy(), color=BLUE, height=0.6)
            axes.set_yticks(range(len(plan_order)))
            axes.set_yticklabels(plan_order, fontsize=8)
            axes.set_title(f"{n_cars} cars", fontsize=10, color=INK_COLOUR)
            axes.set_xlabel(label, fontsize=7.5, color=MUTED_INK_COLOUR)
            style_axes(axes)
    figure.suptitle("Fairness, fixed baseline area, main test days: reported, not enforced "
                    f"({DATA_LABEL})", fontsize=10, color=INK_COLOUR)
    return finish_figure(figure, *folders, "fairness")


def draw_sensitivity_figure(sensitivity, folders):
    """Heat map of the relative gain (percent) for each scenario, hypothesis and fleet size."""
    sensitivity = sensitivity.assign(column_name=lambda table: table["hypothesis"] + " " + table["n_cars"].astype(str))
    gains = sensitivity.pivot(index="scenario", columns="column_name", values="relative_gain_percent")
    passes = sensitivity.pivot(index="scenario", columns="column_name", values="fleet_size_status")
    column_order = (sensitivity.sort_values(["hypothesis", "n_cars"])["column_name"].drop_duplicates().tolist())
    gains = gains.loc[list(SCENARIOS), column_order]
    passes = passes.loc[list(SCENARIOS), column_order]
    limit = float(min(max(np.nanpercentile(np.abs(gains.to_numpy()), 90), 5.0), 50.0))
    colour_map = LinearSegmentedColormap.from_list("orange_grey_blue", [ORANGE, "#f0efec", BLUE])
    figure, axes = plt.subplots(figsize=(15, 4.6))
    image = axes.imshow(gains.to_numpy(), cmap=colour_map, vmin=-limit, vmax=limit, aspect="auto")
    axes.set_xticks(range(gains.shape[1]))
    axes.set_xticklabels(gains.columns, rotation=45, ha="right", fontsize=8)
    axes.set_yticks(range(gains.shape[0]))
    axes.set_yticklabels(gains.index, fontsize=8)
    for row_number, scenario in enumerate(gains.index):
        for column_number, column_name in enumerate(gains.columns):
            mark = "*" if passes.loc[scenario, column_name] == ht.STATUS_PASS else ""
            axes.text(column_number, row_number, f"{gains.loc[scenario, column_name]:.1f}{mark}",
                      ha="center", va="center", fontsize=7.5, color=INK_COLOUR)
    colour_bar = figure.colorbar(image, ax=axes)
    colour_bar.set_label("relative gain of first plan over second plan, % (blue = first plan better)", fontsize=8)
    axes.set_title("Sensitivity of H2-H5 (hypothesis code and number of cars; * = passes the verdict "
                   f"criteria at that fleet size)\n{DATA_LABEL}", fontsize=9, color=INK_COLOUR)
    return finish_figure(figure, *folders, "sensitivity")


def draw_validation_area_figure(tuning_table, frozen, folders):
    """Validation profit per car of each candidate area, one panel per fleet size."""
    areas = tuning_table[tuning_table["step"] == "launch_area"]
    figure, all_axes = plt.subplots(1, len(frozen["fleet_sizes"]), figsize=(15, 4.4))
    for axes, n_cars in zip(all_axes, frozen["fleet_sizes"]):
        rows = areas[areas["n_cars"] == n_cars].reset_index(drop=True)
        chosen_label = frozen["settings_by_fleet_size"][str(n_cars)]["chosen_area_label"]
        colours = [NEUTRAL_GREY if label == BASELINE_LABEL else ORANGE if label == chosen_label else BLUE
                   for label in rows["candidate"]]
        axes.bar(range(len(rows)), rows["mean_profit_per_car_usd"], color=colours, width=0.6)
        axes.set_xticks(range(len(rows)))
        axes.set_xticklabels([label.replace(CANDIDATE_PREFIX, "proxy ").replace("fixed_baseline", "baseline")
                              for label in rows["candidate"]], fontsize=8)
        axes.set_title(f"{n_cars} cars (chosen: {chosen_label}; grey = baseline)", fontsize=9, color=INK_COLOUR)
        axes.set_ylabel("mean validation profit per car per day, USD", fontsize=8, color=MUTED_INK_COLOUR)
        style_axes(axes)
    figure.suptitle("Choosing the launch area on October 2024 validation days", fontsize=10, color=INK_COLOUR)
    return finish_figure(figure, *folders, "validation")


# ================================================================== printing

def print_results_table(hypothesis_summary, price_options, frozen):
    """Print the main results in plain English."""
    print("\n" + "=" * 100)
    print(f"RESULTS (all numbers: {DATA_LABEL})")
    print("Daily profit per car in USD. 'Median difference' = first plan minus second plan, paired by day.")
    print(f"Prices: calibrated {price_options['calibrated']['base_charge_usd']:.2f} + "
          f"{price_options['calibrated']['per_mile_rate_usd']:.2f} per mile (main); "
          f"nominal {NOMINAL_PRICE['base_charge_usd']:.2f} + {NOMINAL_PRICE['per_mile_rate_usd']:.2f} per mile (sensitivity).")
    for family in (FAMILY_PRIMARY, FAMILY_ROBUSTNESS, FAMILY_EXTRA, FAMILY_POSTHOC):
        print("\n" + "-" * 100)
        print(f"Family: {family}")
        family_table = hypothesis_summary[hypothesis_summary["family"] == family]
        for code, group in family_table.groupby("hypothesis", sort=True):
            print(f"\n{code}  {group['description'].iloc[0]}  ->  VERDICT: {group['verdict'].iloc[0]}")
            for row in group.itertuples():
                area_note = ""
                if code == "H2":
                    area_note = f" [chosen area {frozen['settings_by_fleet_size'][str(row.n_cars)]['chosen_area_label']}]"
                print(f"   {row.n_cars:>3} cars: {row.n_days} days, median diff {row.median_difference_usd:+.2f} USD "
                      f"({row.relative_gain_percent:+.2f}%), 95% CI [{row.ci95_lower_usd:+.2f}, {row.ci95_upper_usd:+.2f}], "
                      f"p one-sided raw {row.p_one_sided:.4f}, Holm {row.p_holm_one_sided:.4f}, "
                      f"first plan wins {100 * row.win_share:.0f}% of days -> {row.fleet_size_status}{area_note}")


# ================================================================== main

def main():
    """Validation, freeze, test, statistics, files and figures."""
    started = time.time()
    repo_root = fleet_sim.find_repo_root()
    results_folder = repo_root / RESULTS_FOLDER_NAME
    results_folder.mkdir(parents=True, exist_ok=True)
    folders = (results_folder, repo_root / REPORT_ASSETS_FOLDER_NAME)

    inputs = fleet_sim.load_simulation_inputs(repo_root)
    price_fit = fleet_sim.fit_common_price(inputs["training_zone_hour"])
    price_options = {"calibrated": price_fit, "nominal": NOMINAL_PRICE}
    print(f"Calibrated common price (fit_common_price): {price_fit['base_charge_usd']:.3f} base + "
          f"{price_fit['per_mile_rate_usd']:.3f} per mile (R2 {price_fit['r_squared']:.3f}); "
          f"nominal price of the launch-area proxy: {NOMINAL_PRICE}")
    forecast_path = repo_root / FORECAST_FILE_NAME
    forecast_table = read_forecast_table(repo_root)
    forecasts, fill_report = build_forecasts(forecast_table, inputs["zone_ids"])
    areas, candidate_labels = load_candidate_areas(repo_root)
    print(f"Areas: {BASELINE_LABEL} {len(areas[BASELINE_LABEL])} zones; candidates "
          f"{[(label, len(areas[label])) for label in candidate_labels]}")

    validation_days = pick_days_in_period(inputs, VALIDATION_PERIOD_LABEL, VALIDATION_FIRST_DAY, VALIDATION_LAST_DAY)
    validation_holidays = find_holiday_days(inputs, validation_days)
    validation_days = [day for day in validation_days if day not in validation_holidays]
    test_days = pick_days_in_period(inputs, TEST_PERIOD_LABEL)
    holiday_days = find_holiday_days(inputs, test_days)
    if tuple(holiday_days) != EXPECTED_TEST_HOLIDAYS:
        raise ValueError(f"Test holidays in the calendar are {holiday_days}, expected {EXPECTED_TEST_HOLIDAYS}.")
    check_forecasts_cover_days(forecasts, validation_days + test_days)
    check_actuals_match_simulator(forecast_table, inputs, validation_days)
    print(f"Validation days: {len(validation_days)} (October, holidays {validation_holidays} left out). "
          f"Test days: {len(test_days)} (holidays {len(holiday_days)}, clock-change day 1, rest main).")

    print("\nSTEP 1: validation choices (October only)")
    default_settings = fleet_sim.make_settings(price_fit["base_charge_usd"], price_fit["per_mile_rate_usd"])
    validation_requests = generate_all_requests(inputs, validation_days, default_settings)
    fleet_sizes, choices, tuning_rows = run_validation_choices(
        inputs, validation_requests, forecasts, validation_days, areas, candidate_labels, price_options)
    tuning_table = pd.DataFrame(tuning_rows)
    tuning_table.to_csv(results_folder / "validation_tuning.csv", index=False)
    pd.set_option("display.width", 220, "display.max_columns", 30)
    print(tuning_table.round(3).to_string(index=False))

    if VALIDATION_ONLY_FLAG in sys.argv:
        print("\nStopped after validation (--validation-only): nothing frozen, no test day simulated.")
        return

    print("\nSTEP 2: freeze the settings, before any test day is simulated")
    frozen_path = save_frozen_settings(results_folder, fleet_sizes, choices, areas, price_fit,
                                       validation_days, test_days, holiday_days, forecast_path, len(forecast_table), fill_report)
    frozen = json.loads(frozen_path.read_text())
    print(f"Saved {frozen_path}")
    for n_cars, choice in choices.items():
        print(f"  {n_cars} cars: requests_per_car_hour {choice['requests_per_car_hour']}, "
              f"move_ratio {choice['move_ratio']}, chosen area {choice['chosen_area_label']}")

    print("\nSTEP 3: test period (previously examined, not pristine)")
    check_actuals_match_simulator(forecast_table, inputs, test_days)
    if fill_report:
        print(f"Forecast gaps replaced by the frozen-profile forecast: {fill_report}")
    daily, zone_totals = run_test_period(inputs, forecasts, frozen, areas, test_days, holiday_days)
    daily["data_label"] = DATA_LABEL
    daily.to_csv(results_folder / "daily_results.csv", index=False)

    main_days = [day for day in test_days if label_day_group(day, holiday_days) == MAIN_GROUP]
    weekday_days = [day for day in main_days if not is_weekend_day(day)]
    weekend_days = [day for day in main_days if is_weekend_day(day)]
    print(f"Main test days {len(main_days)} ({len(weekday_days)} weekdays, {len(weekend_days)} weekend days)")
    hypothesis_summary, subgroup_summary, sensitivity_all = build_all_comparison_tables(
        daily, frozen, main_days, weekday_days, weekend_days, holiday_days)
    hypothesis_summary["data_label"] = DATA_LABEL
    hypothesis_summary.to_csv(results_folder / "hypothesis_summary.csv", index=False)
    subgroup_summary["data_label"] = DATA_LABEL
    subgroup_summary.to_csv(results_folder / "subgroup_summary.csv", index=False)
    sensitivity_all["data_label"] = DATA_LABEL
    sensitivity_all.to_csv(results_folder / "sensitivity_summary.csv", index=False)
    plan_summary = build_plan_summary(daily, main_days)
    plan_summary.to_csv(results_folder / "plan_summary.csv", index=False)
    fairness = build_fairness_summary(daily, main_days, zone_totals)
    fairness.to_csv(results_folder / "fairness_summary.csv", index=False)

    draw_paired_differences_figure(daily, frozen, main_days, hypothesis_summary, folders)
    draw_profit_by_plan_figure(plan_summary, frozen, folders)
    draw_fairness_figure(fairness, frozen, folders)
    draw_sensitivity_figure(sensitivity_all, folders)
    draw_validation_area_figure(tuning_table, frozen, folders)

    print_results_table(hypothesis_summary, price_options, frozen)
    print(f"\nFiles saved in {results_folder}; figures also copied to {folders[1]}")
    print(f"Runtime: {time.time() - started:.0f} seconds")


if __name__ == "__main__":
    main()
