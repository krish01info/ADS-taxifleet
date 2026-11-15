"""H2b runner: choose the launch area with the SIMULATOR, then test it against the fixed baseline.

H2b, post hoc, exploratory; previously examined period, not a pristine test.

Where it fits: after notebooks/06_hypothesis_tests.py. That script found H2 NOT SUPPORTED, but its
"data-chosen" areas came from a rough analytical proxy (src/launch_area.py) and were never scored
by the simulator. This script asks the stakeholder question again with a fairer search. The
original H2 result is not touched; every output here has the prefix h2b_.
Run from anywhere:  python notebooks/06b_h2_simulated_area.py
    --search-only   stop after the area is frozen (no test day is simulated)
    --test-only     skip the search and test the area already frozen in data/results/h2b_frozen_area.json

Order of work (the order is what keeps the test honest)
    A. SEARCH on October 2024 validation days only. The score of an area is the mean simulated
       profit per car and day (profit plan, forecast pred_model, 50 cars, calibrated common price,
       requests_per_car_hour and move_ratio frozen for 50 cars, same seed per day). The search is a
       stochastic hill climb over add / remove / swap of one zone, always keeping the area connected
       in the river-links graph, between 30 and 110 zones, at least 30 percent of TRAINING demand,
       no ferry-only zones. It starts from the fixed baseline and from proxy-optimal areas.
       The search itself uses every third validation day; the best areas are then re-scored on
       all validation days and the best of them wins.
    B. FREEZE the winner in data/results/h2b_frozen_area.json. No test day is simulated before.
    C. TEST on the 57 main test days of the main test (same seeds, same frozen settings, fleet sizes
       25 / 50 / 100): simulator-chosen area vs fixed baseline area, profit plan. One-sided paired
       Wilcoxon (chosen > baseline), bootstrap interval, win share, Holm correction over the three
       fleet sizes and the verdict rule of src/hypothesis_tests.py.
    Second check (not part of the verdict): the same comparison with the geofence for DESTINATIONS
    switched off, so a trip may end outside the area. fleet_sim.py is not edited; this script swaps
    one function (keep_requests_inside_area) for the duration of those runs only.
"""

import importlib.util
import json
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import shapely

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import fleet_sim, launch_area
from src import hypothesis_tests as ht

MAIN_TEST_SCRIPT = "notebooks/06_hypothesis_tests.py"

# ------------------------------------------------------------------ files and labels
RESULTS_FOLDER_NAME = "data/results"
REPORT_ASSETS_FOLDER_NAME = "docs/report_assets"
FROZEN_SETTINGS_FILE_NAME = "frozen_settings.json"
MAIN_DAILY_RESULTS_FILE_NAME = "daily_results.csv"
SEARCH_LOG_FILE_NAME = "h2b_search_log.csv"
FROZEN_AREA_FILE_NAME = "h2b_frozen_area.json"
TEST_RESULTS_FILE_NAME = "h2b_test_results.csv"
DAILY_RESULTS_FILE_NAME = "h2b_daily_results.csv"
GEOFENCE_SUMMARY_FILE_NAME = "h2b_geofence_summary.csv"
MAP_FIGURE_FILE_NAME = "fig_h2b_area_map.png"
DIFFERENCES_FIGURE_FILE_NAME = "fig_h2b_paired_differences.png"
DATA_LABEL = "H2b, post hoc, exploratory; previously examined period, not a pristine test"
SEARCH_ONLY_FLAG = "--search-only"
TEST_ONLY_FLAG = "--test-only"

BASELINE_LABEL = "fixed_baseline"
CHOSEN_LABEL = "simulator_chosen"
SEED_PROXY_LABELS = ("proxy_optimal_60", "proxy_optimal_71")
SEED_BASELINE_NAME = "fixed_baseline"
SEARCH_STAGE_SEEDS = "seed_climb"
SEARCH_STAGE_RESTART = "best_restart"
SEARCH_STAGE_FINAL = "all_days_rescore"
SCENARIO_GEOFENCE_ON = "main"
SCENARIO_GEOFENCE_OFF = "geofence_off_destinations"
PLAN_NAME = "profit_model"

# ------------------------------------------------------------------ search settings
SEARCH_FLEET_SIZE = 50
SEARCH_DAY_STEP = 3
MIN_ZONES = 30
MAX_ZONES = 110
MIN_TRAINING_DEMAND_SHARE = 0.30
SEARCH_BUDGET_SECONDS = 25 * 60
SEED_CLIMB_BUDGET_SHARE = 0.8
CANDIDATES_PER_ROUND = 40
ROUNDS_WITHOUT_GAIN_LIMIT = 8
RESTART_CANDIDATES_PER_ROUND = 100
RESTART_ROUNDS_WITHOUT_GAIN_LIMIT = 25
FINALIST_COUNT = 12
SEARCH_RNG_SEED = 20261117
SCORE_IMPROVEMENT_TOLERANCE = 1e-9
PROGRESS_EVERY_SECONDS = 60

# ------------------------------------------------------------------ figures
FIGURE_DPI = 140
SURFACE_COLOUR = "#fcfcfb"
INK_COLOUR = "#0b0b0b"
MUTED_INK_COLOUR = "#52514e"
GRID_COLOUR = "#e1e0d9"
NEUTRAL_GREY = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
FLEET_COLOURS = (BLUE, ORANGE, AQUA)
JITTER_WIDTH = 0.12
MAP_PANEL_COLOURS = {"both": AQUA, "baseline_only": BLUE, "chosen_only": ORANGE}
REPRODUCTION_TOLERANCE_USD = 1e-6
MIN_DAYS_FOR_TEST = 6


def load_main_test_module():
    """Import notebooks/06_hypothesis_tests.py as a module so its set-up code is reused, not copied."""
    spec = importlib.util.spec_from_file_location("main_hypothesis_test", REPO_ROOT / MAIN_TEST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main_test = load_main_test_module()


# ================================================================== set-up

def load_setup():
    """Load simulator inputs, frozen settings, forecasts, zone graph and training demand shares."""
    inputs = fleet_sim.load_simulation_inputs(REPO_ROOT)
    frozen_path = REPO_ROOT / RESULTS_FOLDER_NAME / FROZEN_SETTINGS_FILE_NAME
    frozen = json.loads(frozen_path.read_text())
    price_options = {"calibrated": frozen["price_calibrated"], "nominal": frozen["price_nominal"]}
    forecast_table = main_test.read_forecast_table(REPO_ROOT)
    forecast_hash = main_test.hash_file(REPO_ROOT / frozen["forecast_file"]["path"])
    if forecast_hash != frozen["forecast_file"]["sha256"]:
        raise ValueError("The forecast file differs from the one named in frozen_settings.json.")
    forecasts, _ = main_test.build_forecasts(forecast_table, inputs["zone_ids"])
    zones = launch_area.load_zones(REPO_ROOT)
    polygon_neighbours = launch_area.load_polygon_neighbours(REPO_ROOT)
    river_links = launch_area.select_bridge_links([launch_area.LINK_TYPE_RIVER_CROSSING])
    graph = launch_area.build_zone_graph(zones, polygon_neighbours, river_links)
    mean_hourly_requests = launch_area.load_training_mean_hourly_requests(REPO_ROOT)
    zone_demand_share = launch_area.make_zone_demand_share(mean_hourly_requests)
    print(f"Set-up: {len(zones)} zones, graph river_links, forecast file hash matches, "
          f"training demand shares for {len(zone_demand_share)} zones.")
    return {"inputs": inputs, "frozen": frozen, "frozen_path": frozen_path,
            "price_options": price_options, "forecasts": forecasts, "forecast_table": forecast_table,
            "zones": zones, "graph": graph, "zone_demand_share": zone_demand_share}


def parse_days(day_texts):
    """Turn a list of ISO date strings from the frozen file into date objects."""
    return [datetime.fromisoformat(text).date() for text in day_texts]


def make_fleet_settings(setup, n_cars):
    """Simulator settings (calibrated price, frozen requests_per_car_hour and move_ratio) for one fleet size."""
    fleet_choice = setup["frozen"]["settings_by_fleet_size"][str(n_cars)]
    return main_test.make_scenario_settings(setup["price_options"], main_test.MAIN_SCENARIO, fleet_choice)


# ================================================================== A. search

def make_area_score_function(setup, requests_by_day, days, n_cars):
    """Return score(area_zone_ids) = mean simulated profit per car per day over days (profit plan)."""
    inputs = setup["inputs"]
    settings = make_fleet_settings(setup, n_cars)
    forecast_by_day = setup["forecasts"]["pred_model"]
    seed = setup["frozen"]["simulation_seed"]

    def score_area(area_zone_ids):
        profits = []
        for day in days:
            summary, _ = fleet_sim.simulate_day(inputs, requests_by_day[day], forecast_by_day[day], "profit",
                                                n_cars, area_zone_ids, settings, seed)
            profits.append(summary["profit_per_car_usd"])
        return float(np.mean(profits))

    return score_area


def make_logged_scorer(score_function, zone_demand_share, search_log, state):
    """Cache scores per area and append one log row each time an area is scored for the first time."""
    cache = {}

    def scorer(area_zone_ids):
        key = frozenset(int(zone_id) for zone_id in area_zone_ids)
        if key not in cache:
            cache[key] = float(score_function(sorted(key)))
            search_log.append({
                "eval_order": len(search_log) + 1, "stage": state["stage"], "start_seed": state["seed_name"],
                "round": state["round"], "n_zones": len(key),
                "training_demand_share": launch_area.demand_share_of_area(key, zone_demand_share),
                "mean_profit_per_car_usd": cache[key],
                "seconds_since_start": time.time() - state["started"],
                "zone_ids": " ".join(str(zone_id) for zone_id in sorted(key)),
            })
        return cache[key]

    return scorer, cache


def sample_allowed_moves(setup, area, rng, count):
    """Pick up to count random one-zone changes (add, remove, swap) that keep the area allowed."""
    graph, zone_demand_share = setup["graph"], setup["zone_demand_share"]
    all_moves = launch_area.list_neighbour_areas(graph, area, MIN_ZONES, MAX_ZONES)
    allowed_moves = []
    for move_number in rng.permutation(len(all_moves)):
        candidate = all_moves[move_number]
        if launch_area.area_is_allowed(graph, candidate, MIN_ZONES, MAX_ZONES, zone_demand_share,
                                       MIN_TRAINING_DEMAND_SHARE):
            allowed_moves.append(candidate)
            if len(allowed_moves) == count:
                break
    return allowed_moves


def climb_from_area(setup, scorer, state, start_area, rng, deadline, candidates_per_round, rounds_limit):
    """Hill climb: score a random sample of one-zone changes, take the best if it beats the current area.

    Stops after rounds_limit rounds in a row without gain, or at the deadline. Returns the final area.
    """
    area = set(start_area)
    rounds_without_gain = 0
    while rounds_without_gain < rounds_limit and time.time() < deadline:
        state["round"] += 1
        candidates = sample_allowed_moves(setup, area, rng, candidates_per_round)
        if not candidates:
            break
        best_candidate = max(candidates, key=scorer)
        if scorer(best_candidate) > scorer(area) + SCORE_IMPROVEMENT_TOLERANCE:
            area = set(best_candidate)
            rounds_without_gain = 0
        else:
            rounds_without_gain += 1
        if time.time() - state["last_print"] > PROGRESS_EVERY_SECONDS:
            state["last_print"] = time.time()
            print(f"  [{state['seed_name']}] round {state['round']}: {len(area)} zones, "
                  f"score {scorer(area):.3f}, areas scored so far {state['count_function']()}", flush=True)
    return area


def build_seed_areas(setup):
    """The areas the search starts from: the fixed baseline and the proxy-optimal areas (river-links graph)."""
    areas, _ = main_test.load_candidate_areas(REPO_ROOT)
    seed_areas = {SEED_BASELINE_NAME: launch_area.fixed_baseline_area(setup["zones"])}
    for label in SEED_PROXY_LABELS:
        seed_areas[label] = areas[label]
    if seed_areas[SEED_BASELINE_NAME] != setup["frozen"]["baseline_area_zone_ids"]:
        raise ValueError("The fixed baseline differs from the baseline frozen for the main test.")
    for name, zone_ids in seed_areas.items():
        allowed = launch_area.area_is_allowed(setup["graph"], set(zone_ids), MIN_ZONES, MAX_ZONES,
                                              setup["zone_demand_share"], MIN_TRAINING_DEMAND_SHARE)
        if not allowed or set(zone_ids) & set(launch_area.FERRY_ONLY_ZONE_IDS):
            raise ValueError(f"Seed area {name} breaks the search constraints.")
    return seed_areas


def run_search(setup, search_score_function, seed_areas):
    """Climb from every seed area, then restart from the best area found. Returns (log rows, cache, summary)."""
    search_log = []
    started = time.time()
    state = {"stage": SEARCH_STAGE_SEEDS, "seed_name": "", "round": 0, "started": started,
             "last_print": started, "count_function": lambda: len(search_log)}
    scorer, cache = make_logged_scorer(search_score_function, setup["zone_demand_share"], search_log, state)
    rng = np.random.default_rng(SEARCH_RNG_SEED)
    seed_deadline = started + SEED_CLIMB_BUDGET_SHARE * SEARCH_BUDGET_SECONDS
    end_deadline = started + SEARCH_BUDGET_SECONDS
    seed_results = {}
    for position, (name, start_area) in enumerate(seed_areas.items()):
        state["seed_name"] = name
        state["round"] = 0
        scorer(start_area)
        deadline = time.time() + (seed_deadline - time.time()) / (len(seed_areas) - position)
        final_area = climb_from_area(setup, scorer, state, start_area, rng, deadline,
                                     CANDIDATES_PER_ROUND, ROUNDS_WITHOUT_GAIN_LIMIT)
        seed_results[name] = {"start_score": scorer(start_area), "end_score": scorer(final_area),
                              "end_zones": len(final_area)}
        print(f"Seed {name}: start {scorer(start_area):.3f} -> end {scorer(final_area):.3f} "
              f"({len(final_area)} zones); areas scored so far {len(search_log)}", flush=True)
    best_key = max(cache, key=cache.get)
    state.update({"stage": SEARCH_STAGE_RESTART, "seed_name": "best_so_far", "round": 0})
    restart_area = climb_from_area(setup, scorer, state, best_key, rng, end_deadline,
                                   RESTART_CANDIDATES_PER_ROUND, RESTART_ROUNDS_WITHOUT_GAIN_LIMIT)
    print(f"Restart from best: {scorer(best_key):.3f} -> {scorer(restart_area):.3f} "
          f"({len(restart_area)} zones)", flush=True)
    summary = {"seed_results": seed_results, "areas_scored_in_search": len(search_log),
               "search_seconds": time.time() - started}
    return search_log, cache, summary


def rescore_finalists(cache, all_days_score_function, baseline_area, search_log, setup):
    """Re-score the best searched areas (and the baseline) on ALL validation days. Returns a table of finalists."""
    baseline_key = frozenset(baseline_area)
    ranked = sorted((key for key in cache if key != baseline_key), key=lambda key: -cache[key])
    finalist_keys = ranked[:FINALIST_COUNT]
    rows = []
    for key in [baseline_key] + finalist_keys:
        full_score = all_days_score_function(sorted(key))
        rows.append({"area_key": key, "search_score": cache[key], "all_days_score": full_score})
        search_log.append({
            "eval_order": len(search_log) + 1, "stage": SEARCH_STAGE_FINAL,
            "start_seed": "baseline" if key == baseline_key else "finalist", "round": 0,
            "n_zones": len(key),
            "training_demand_share": launch_area.demand_share_of_area(key, setup["zone_demand_share"]),
            "mean_profit_per_car_usd": full_score, "seconds_since_start": np.nan,
            "zone_ids": " ".join(str(zone_id) for zone_id in sorted(key))})
    return pd.DataFrame(rows)


def count_boroughs(zones, zone_ids):
    """Number of zones per borough as a dict, biggest first."""
    counts = zones[zones["zone_id"].isin(zone_ids)]["borough"].value_counts()
    return {borough: int(number) for borough, number in counts.items()}


def pick_winner_row(finalists):
    """The searched area (not the baseline, which is row 0) with the best all-days validation score."""
    candidates = finalists.iloc[1:]
    return candidates.loc[candidates["all_days_score"].idxmax()]


def build_frozen_area_record(setup, finalists, search_summary, validation_days, search_days, by_fleet_size):
    """Everything about the winning area that is written to h2b_frozen_area.json before any test day."""
    baseline_zone_ids = launch_area.fixed_baseline_area(setup["zones"])
    candidates = finalists.iloc[1:]
    winner_row = pick_winner_row(finalists)
    winner_zone_ids = sorted(int(zone_id) for zone_id in winner_row["area_key"])
    share = setup["zone_demand_share"]
    return {
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "data_label": DATA_LABEL,
        "test_days_simulated_before_this_file_was_written": False,
        "area_label": CHOSEN_LABEL,
        "zone_ids": winner_zone_ids,
        "n_zones": len(winner_zone_ids),
        "boroughs": count_boroughs(setup["zones"], winner_zone_ids),
        "training_demand_share": launch_area.demand_share_of_area(winner_zone_ids, share),
        "baseline_zone_ids": baseline_zone_ids,
        "baseline_n_zones": len(baseline_zone_ids),
        "baseline_boroughs": count_boroughs(setup["zones"], baseline_zone_ids),
        "baseline_training_demand_share": launch_area.demand_share_of_area(baseline_zone_ids, share),
        "zones_in_both": len(set(winner_zone_ids) & set(baseline_zone_ids)),
        "validation_profit_per_car_usd_search_fleet_all_days": {
            "fleet_size": SEARCH_FLEET_SIZE, "chosen": float(winner_row["all_days_score"]),
            "baseline": float(finalists.iloc[0]["all_days_score"]),
            "chosen_search_days_only": float(winner_row["search_score"]),
            "baseline_search_days_only": float(finalists.iloc[0]["search_score"])},
        "validation_profit_per_car_usd_by_fleet_size_all_days": by_fleet_size,
        "finalists_all_days_scores": [float(value) for value in candidates["all_days_score"]],
        "search": {
            "seed_areas": list(SEED_PROXY_LABELS) + [SEED_BASELINE_NAME],
            "constraints": {"min_zones": MIN_ZONES, "max_zones": MAX_ZONES,
                            "min_training_demand_share": MIN_TRAINING_DEMAND_SHARE,
                            "excluded_ferry_only_zone_ids": list(launch_area.FERRY_ONLY_ZONE_IDS),
                            "graph": "river_links, connected area"},
            "method": "stochastic hill climb over add / remove / swap of one zone, scored by the simulator",
            "search_days": [day.isoformat() for day in search_days],
            "all_validation_days": [day.isoformat() for day in validation_days],
            "fleet_size": SEARCH_FLEET_SIZE,
            "rng_seed": SEARCH_RNG_SEED,
            **search_summary},
        "frozen_settings_file_sha256": main_test.hash_file(setup["frozen_path"]),
    }


def search_and_freeze(setup, results_folder):
    """Steps A and B: search on validation days, then write the frozen area file. Returns the record."""
    frozen = setup["frozen"]
    validation_days = parse_days(frozen["validation_days"])
    search_days = validation_days[::SEARCH_DAY_STEP]
    main_test.check_forecasts_cover_days(setup["forecasts"], validation_days)
    main_test.check_actuals_match_simulator(setup["forecast_table"], setup["inputs"], validation_days)
    request_settings = fleet_sim.make_settings(0.0, 0.0, demand_scale=frozen["demand_scale"])
    requests_by_day = main_test.generate_all_requests(setup["inputs"], validation_days, request_settings)
    search_score = make_area_score_function(setup, requests_by_day, search_days, SEARCH_FLEET_SIZE)
    all_days_score = make_area_score_function(setup, requests_by_day, validation_days, SEARCH_FLEET_SIZE)
    seed_areas = build_seed_areas(setup)
    print(f"\nSTEP A: search on {len(search_days)} of {len(validation_days)} October days, "
          f"{SEARCH_FLEET_SIZE} cars, budget {SEARCH_BUDGET_SECONDS // 60} minutes", flush=True)
    search_log, cache, search_summary = run_search(setup, search_score, seed_areas)
    finalists = rescore_finalists(cache, all_days_score, seed_areas[SEED_BASELINE_NAME], search_log, setup)
    pd.DataFrame(search_log).assign(data_label=DATA_LABEL).to_csv(results_folder / SEARCH_LOG_FILE_NAME, index=False)
    by_fleet_size = {}
    winner_zone_ids = sorted(int(zone_id) for zone_id in pick_winner_row(finalists)["area_key"])
    for n_cars in frozen["fleet_sizes"]:
        score_function = make_area_score_function(setup, requests_by_day, validation_days, n_cars)
        by_fleet_size[str(n_cars)] = {"chosen": score_function(winner_zone_ids),
                                      "baseline": score_function(seed_areas[SEED_BASELINE_NAME])}
    record = build_frozen_area_record(setup, finalists, search_summary, validation_days, search_days,
                                      by_fleet_size)
    print("\nSTEP B: freeze the area (before any test day)")
    (results_folder / FROZEN_AREA_FILE_NAME).write_text(json.dumps(record, indent=2))
    scores = record["validation_profit_per_car_usd_search_fleet_all_days"]
    print(f"Frozen {record['n_zones']} zones {record['boroughs']}, training demand share "
          f"{record['training_demand_share']:.3f}; validation profit per car {scores['chosen']:.2f} vs "
          f"baseline {scores['baseline']:.2f} USD ({record['search']['areas_scored_in_search']} areas scored "
          f"in {record['search']['search_seconds']:.0f} s)", flush=True)
    return record


# ================================================================== C. test

def keep_requests_starting_inside_area(requests, is_allowed):
    """Geofence for pickups only: keep requests that start in an allowed zone, whatever their destination."""
    keep = is_allowed[requests["origin"]]
    request_columns = ["slot", "origin", "destination", "trip_miles", "trip_minutes", "toll_usd"]
    return {name: requests[name][keep] for name in request_columns}


@contextmanager
def destination_geofence_switched_off():
    """For the duration of a with-block the simulator keeps requests that end outside the area."""
    original_function = fleet_sim.keep_requests_inside_area
    fleet_sim.keep_requests_inside_area = keep_requests_starting_inside_area
    try:
        yield
    finally:
        fleet_sim.keep_requests_inside_area = original_function


def simulate_test_days(setup, area_record, test_days):
    """Simulate the profit plan in both areas, three fleet sizes, geofence on and off. Returns the daily table."""
    frozen = setup["frozen"]
    request_settings = fleet_sim.make_settings(0.0, 0.0, demand_scale=frozen["demand_scale"])
    requests_by_day = main_test.generate_all_requests(setup["inputs"], test_days, request_settings)
    day_groups = {day: main_test.MAIN_GROUP for day in test_days}
    areas = {BASELINE_LABEL: area_record["baseline_zone_ids"], CHOSEN_LABEL: area_record["zone_ids"]}
    rows = []
    for scenario in (SCENARIO_GEOFENCE_ON, SCENARIO_GEOFENCE_OFF):
        for n_cars in frozen["fleet_sizes"]:
            settings = make_fleet_settings(setup, n_cars)
            for area_label, zone_ids in areas.items():
                if scenario == SCENARIO_GEOFENCE_OFF:
                    with destination_geofence_switched_off():
                        new_rows = main_test.run_plans(
                            setup["inputs"], requests_by_day, setup["forecasts"], day_groups, [PLAN_NAME],
                            n_cars, area_label, zone_ids, settings, scenario, main_test.TEST_PERIOD_LABEL)
                else:
                    new_rows = main_test.run_plans(
                        setup["inputs"], requests_by_day, setup["forecasts"], day_groups, [PLAN_NAME],
                        n_cars, area_label, zone_ids, settings, scenario, main_test.TEST_PERIOD_LABEL)
                rows += new_rows
        print(f"  scenario {scenario}: done ({len(rows)} rows so far)", flush=True)
    daily = pd.DataFrame(rows)
    daily["data_label"] = DATA_LABEL
    return daily


def check_baseline_matches_main_test(daily, results_folder, fleet_sizes):
    """Stop if the baseline daily profits differ from the main test's daily_results.csv (same seeds, same code)."""
    main_daily = pd.read_csv(results_folder / MAIN_DAILY_RESULTS_FILE_NAME)
    main_daily = main_daily[(main_daily["scenario"] == main_test.MAIN_SCENARIO)
                            & (main_daily["plan"] == PLAN_NAME)
                            & (main_daily["area_label"] == BASELINE_LABEL)]
    mine = daily[(daily["scenario"] == SCENARIO_GEOFENCE_ON) & (daily["area_label"] == BASELINE_LABEL)]
    for n_cars in fleet_sizes:
        theirs = main_daily[main_daily["n_cars"] == n_cars].set_index("date")["profit_per_car_usd"]
        ours = mine[mine["n_cars"] == n_cars].assign(date_text=lambda table: table["date"].astype(str))
        ours = ours.set_index("date_text")["profit_per_car_usd"]
        largest_difference = float((ours - theirs.reindex(ours.index)).abs().max())
        if not largest_difference < REPRODUCTION_TOLERANCE_USD:
            raise ValueError(f"Baseline profits for {n_cars} cars differ from the main test by "
                             f"{largest_difference} USD; the set-up is not the same.")
    print("Check passed: baseline daily profits equal the main test's daily_results.csv for every fleet size.")


def compare_areas(daily, scenario, fleet_sizes, days):
    """Chosen vs baseline for every fleet size: paired statistics, Holm over the fleet sizes, verdict."""
    rows = []
    for n_cars in fleet_sizes:
        chosen = main_test.get_plan_series(daily, scenario, n_cars, CHOSEN_LABEL, PLAN_NAME, days)
        baseline = main_test.get_plan_series(daily, scenario, n_cars, BASELINE_LABEL, PLAN_NAME, days)
        statistics = ht.compare_two_plans(chosen, baseline, [main_test.BOOTSTRAP_SEED, n_cars, 0])
        rows.append({"scenario": scenario, "hypothesis": "H2b",
                     "description": "simulator-chosen area vs fixed baseline area (profit plan)",
                     "n_cars": n_cars, **statistics})
    table = pd.DataFrame(rows)
    table["p_holm_one_sided"] = ht.holm_adjust(table["p_one_sided"].to_numpy())
    table["fleet_size_status"] = [
        ht.classify_fleet_size(row.p_holm_one_sided, row.median_difference_usd, row.relative_gain_percent,
                               row.ci95_lower_usd, row.ci95_upper_usd) for row in table.itertuples()]
    table["verdict"] = ht.decide_verdict(table["fleet_size_status"])
    table["data_label"] = DATA_LABEL
    return table


def summarise_geofence(daily, days):
    """Per scenario, fleet size and area: pooled share of requests lost to the geofence and served share."""
    rows = []
    selected = daily[daily["date"].isin(days)]
    for (scenario, n_cars, area_label), group in selected.groupby(["scenario", "n_cars", "area_label"]):
        in_area = group["requests_in_area"].sum()
        rows.append({
            "scenario": scenario, "n_cars": n_cars, "area_label": area_label, "n_days": len(group),
            "requests_in_area_per_day": in_area / len(group),
            "share_of_all_city_requests_starting_in_area": in_area / group["requests_all_zones"].sum(),
            "requests_dropped_outside_per_day": group["requests_dropped_outside"].sum() / len(group),
            "dropped_outside_share_of_area_requests": group["requests_dropped_outside"].sum() / in_area,
            "served_share_of_area_requests": group["served"].sum() / in_area,
            "mean_utilisation": group["utilisation"].mean(),
            "mean_empty_mile_share": group["empty_mile_share"].mean(),
            "median_profit_per_car_usd": group["profit_per_car_usd"].median(),
            "data_label": DATA_LABEL})
    return pd.DataFrame(rows)


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


def save_figure(figure, file_name):
    """Save a figure in data/results and copy it to docs/report_assets."""
    figure.patch.set_facecolor(SURFACE_COLOUR)
    figure.tight_layout()
    results_path = REPO_ROOT / RESULTS_FOLDER_NAME / file_name
    figure.savefig(results_path, dpi=FIGURE_DPI, facecolor=SURFACE_COLOUR)
    plt.close(figure)
    assets_folder = REPO_ROOT / REPORT_ASSETS_FOLDER_NAME
    assets_folder.mkdir(parents=True, exist_ok=True)
    (assets_folder / file_name).write_bytes(results_path.read_bytes())


def load_zone_polygons():
    """Zone id -> shapely polygon, read from the zones table of the release."""
    geometry_table = launch_area.read_parquet_directory(
        launch_area.release_table_directory(REPO_ROOT, "zones"), "zone_id, geometry_wkb")
    return {int(row.zone_id): shapely.from_wkb(bytes(row.geometry_wkb)) for row in geometry_table.itertuples()}


def draw_area_map(area_record):
    """One map: zones in both areas, baseline only, chosen only, and all other zones in grey."""
    baseline, chosen = set(area_record["baseline_zone_ids"]), set(area_record["zone_ids"])
    figure, axes = plt.subplots(figsize=(7.5, 8))
    for zone_id, polygon in load_zone_polygons().items():
        if zone_id in baseline and zone_id in chosen:
            colour = MAP_PANEL_COLOURS["both"]
        elif zone_id in baseline:
            colour = MAP_PANEL_COLOURS["baseline_only"]
        elif zone_id in chosen:
            colour = MAP_PANEL_COLOURS["chosen_only"]
        else:
            colour = GRID_COLOUR
        for part in (polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]):
            x_values, y_values = part.exterior.xy
            axes.fill(x_values, y_values, facecolor=colour, edgecolor=SURFACE_COLOUR, linewidth=0.3)
    axes.set_aspect("equal")
    axes.axis("off")
    legend_text = {"both": f"in both ({len(baseline & chosen)} zones)",
                   "baseline_only": f"fixed baseline only ({len(baseline - chosen)})",
                   "chosen_only": f"simulator-chosen only ({len(chosen - baseline)})"}
    for key, text in legend_text.items():
        axes.fill([], [], color=MAP_PANEL_COLOURS[key], label=text)
    axes.legend(loc="upper left", fontsize=8, frameon=False)
    axes.set_title(f"Fixed baseline ({len(baseline)} zones) vs simulator-chosen area ({len(chosen)} zones)\n"
                   f"chosen on October validation days. {DATA_LABEL}", fontsize=8.5, color=INK_COLOUR)
    save_figure(figure, MAP_FIGURE_FILE_NAME)


def draw_differences_figure(daily, test_results, fleet_sizes, days):
    """Daily paired differences (chosen minus baseline), geofence on (left) and destinations free (right)."""
    figure, all_axes = plt.subplots(1, 2, figsize=(11, 4.8))
    rng = np.random.default_rng(main_test.BOOTSTRAP_SEED)
    for axes, scenario, title in zip(all_axes, (SCENARIO_GEOFENCE_ON, SCENARIO_GEOFENCE_OFF),
                                     ("main test (geofence on)", "check: destination geofence off")):
        for position, n_cars in enumerate(fleet_sizes):
            chosen = main_test.get_plan_series(daily, scenario, n_cars, CHOSEN_LABEL, PLAN_NAME, days)
            baseline = main_test.get_plan_series(daily, scenario, n_cars, BASELINE_LABEL, PLAN_NAME, days)
            differences = chosen - baseline
            colour = FLEET_COLOURS[position]
            axes.boxplot(differences, positions=[position], widths=0.5, showfliers=False,
                         medianprops={"color": INK_COLOUR, "linewidth": 1.6}, boxprops={"color": colour},
                         whiskerprops={"color": colour}, capprops={"color": colour})
            jitter = rng.uniform(-JITTER_WIDTH, JITTER_WIDTH, len(differences))
            axes.scatter(position + jitter, differences, s=9, color=colour, alpha=0.6, zorder=3)
        axes.axhline(0, color=MUTED_INK_COLOUR, linewidth=1.0)
        verdict = test_results[test_results["scenario"] == scenario]["verdict"].iloc[0]
        axes.set_title(f"{title}: verdict {verdict}", fontsize=9, color=INK_COLOUR)
        axes.set_xticks(range(len(fleet_sizes)))
        axes.set_xticklabels([f"{n_cars} cars" for n_cars in fleet_sizes])
        axes.set_ylabel("chosen minus baseline, USD per car per day", fontsize=8, color=MUTED_INK_COLOUR)
        style_axes(axes)
    figure.suptitle(f"H2b paired daily profit differences, {len(days)} main test days ({DATA_LABEL})",
                    fontsize=9, color=INK_COLOUR)
    save_figure(figure, DIFFERENCES_FIGURE_FILE_NAME)


# ================================================================== printing and main

def print_test_results(test_results, geofence_summary):
    """Print the H2b table and the geofence numbers in plain English."""
    print("\n" + "=" * 100)
    print(f"H2b RESULTS ({DATA_LABEL})")
    for scenario, group in test_results.groupby("scenario", sort=False):
        print(f"\nScenario {scenario}: VERDICT {group['verdict'].iloc[0]}")
        for row in group.itertuples():
            print(f"  {row.n_cars:>3} cars: median diff {row.median_difference_usd:+.2f} USD "
                  f"({row.relative_gain_percent:+.2f}%), 95% CI [{row.ci95_lower_usd:+.2f}, "
                  f"{row.ci95_upper_usd:+.2f}], p one-sided {row.p_one_sided:.4g}, Holm "
                  f"{row.p_holm_one_sided:.4g}, chosen wins {100 * row.win_share:.0f}% of days "
                  f"-> {row.fleet_size_status}")
    pd.set_option("display.width", 220, "display.max_columns", 30)
    print("\nGeofence and served share (pooled over test days):")
    print(geofence_summary.drop(columns=["data_label"]).round(3).to_string(index=False))


def run_test(setup, area_record, results_folder):
    """Step C: simulate the test days, compare the areas, write tables and figures."""
    frozen = setup["frozen"]
    test_days = parse_days(frozen["test_days_main_group"])
    if len(test_days) < MIN_DAYS_FOR_TEST:
        raise ValueError("Too few test days for a paired test.")
    main_test.check_forecasts_cover_days(setup["forecasts"], test_days)
    main_test.check_actuals_match_simulator(setup["forecast_table"], setup["inputs"], test_days)
    print(f"\nSTEP C: test on {len(test_days)} main test days, fleet sizes {frozen['fleet_sizes']}", flush=True)
    daily = simulate_test_days(setup, area_record, test_days)
    check_baseline_matches_main_test(daily, results_folder, frozen["fleet_sizes"])
    daily.to_csv(results_folder / DAILY_RESULTS_FILE_NAME, index=False)
    test_results = pd.concat([compare_areas(daily, scenario, frozen["fleet_sizes"], test_days)
                              for scenario in (SCENARIO_GEOFENCE_ON, SCENARIO_GEOFENCE_OFF)])
    test_results.to_csv(results_folder / TEST_RESULTS_FILE_NAME, index=False)
    geofence_summary = summarise_geofence(daily, test_days)
    geofence_summary.to_csv(results_folder / GEOFENCE_SUMMARY_FILE_NAME, index=False)
    draw_area_map(area_record)
    draw_differences_figure(daily, test_results, frozen["fleet_sizes"], test_days)
    print_test_results(test_results, geofence_summary)


def main():
    """Search and freeze the area, then test it (each part can be run on its own)."""
    started = time.time()
    results_folder = REPO_ROOT / RESULTS_FOLDER_NAME
    results_folder.mkdir(parents=True, exist_ok=True)
    setup = load_setup()
    frozen_area_path = results_folder / FROZEN_AREA_FILE_NAME
    if TEST_ONLY_FLAG in sys.argv:
        area_record = json.loads(frozen_area_path.read_text())
        print(f"Using the area frozen at {area_record['frozen_at_utc']} ({area_record['n_zones']} zones).")
    else:
        area_record = search_and_freeze(setup, results_folder)
    if SEARCH_ONLY_FLAG in sys.argv:
        print("Stopped after freezing (--search-only): no test day was simulated.")
    else:
        run_test(setup, area_record, results_folder)
    print(f"\nRuntime: {time.time() - started:.0f} seconds")


if __name__ == "__main__":
    main()
