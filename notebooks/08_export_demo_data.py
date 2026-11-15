"""Phase 8 demo: export one simulated day and the project results into a single offline HTML page.

Where it fits: the viva needs a page the team can open by double-click and use live. This script
runs the fleet simulator (src/fleet_sim.py, not edited) for ONE test day (Tue 12 Nov 2024) with
the frozen settings from Phase 6, records every car move and every request while the simulator
runs, checks that the recorded log gives back the simulator's own summary numbers, reads the
result tables, and writes
    data/demo/demo_data.json            all numbers the page shows (git-ignored)
    docs/demo/simulation_demo.html      docs/demo/template.html with the JSON put inside

How the log is captured: the simulator calls small helper functions (find_car_for_request,
serve_request, drive_car_empty, apply_moves, apply_wait_or_move_rule). For one run we swap those
helpers for thin wrappers that call the real helper and then write down what happened. The
simulator code itself is not changed, and the wrappers are removed again after each run.

Run from anywhere:   python notebooks/08_export_demo_data.py
"""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from shapely import wkb
from shapely.ops import unary_union

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src import fleet_sim  # noqa: E402

# ------------------------------------------------------------------ files
MAIN_TEST_SCRIPT = "notebooks/06_hypothesis_tests.py"
TABLE_INDEX_FILE = "data/releases/phase1-2024-v1/metadata/table_index.json"
FROZEN_SETTINGS_FILE = "data/results/frozen_settings.json"
H2B_AREA_FILE = "data/results/h2b_frozen_area.json"
DAILY_RESULTS_FILE = "data/results/daily_results.csv"
H2B_DAILY_FILE = "data/results/h2b_daily_results.csv"
HYPOTHESIS_SUMMARY_FILE = "data/results/hypothesis_summary.csv"
H2B_TEST_FILE = "data/results/h2b_test_results.csv"
WEEKLY_ERROR_FILE = "data/drift/weekly_error.csv"
DRIFT_SUMMARY_FILE = "data/drift/drift_summary.json"
FORECAST_SUMMARY_FILE = "data/forecasts/forecast_summary.json"
TEMPLATE_FILE = "docs/demo/template.html"
HTML_FILE = "docs/demo/simulation_demo.html"
JSON_FILE = "data/demo/demo_data.json"
PLACEHOLDER = "__DEMO_DATA_JSON__"

# ------------------------------------------------------------------ the demo day and plans
DEMO_DAY = date(2024, 11, 12)
DEMO_DAY_LABEL = "Tuesday 12 November 2024"
BASELINE_KEY = "baseline"
CHOSEN_KEY = "chosen"
BASELINE_LABEL = "fixed_baseline"
CHOSEN_LABEL = "simulator_chosen"
# plan key -> (name in daily_results.csv, simulator policy, forecast column the policy may see, label)
PLANS = {
    "stay": ("stay_model", "stay", "pred_model", "Stay put"),
    "wait_or_move": ("wait_or_move_model", "wait_or_move", "pred_model", "Wait or move to a busier neighbour"),
    "ride_count": ("ride_count_model", "ride_count", "pred_model", "Ride-count placement"),
    "profit": ("profit_model", "profit", "pred_model", "Profit placement (model forecast)"),
    "profit_last_week": ("profit_last_week", "profit", "pred_last_week", "Profit placement (copy last week)"),
}
RESULTS_TOLERANCE = 1e-6

# ------------------------------------------------------------------ the log
EMPTY_KIND = 0
LOADED_KIND = 1
CODE_SERVED = "0"
CODE_NO_CAR = "1"
CODE_OUTSIDE = "2"
MINUTE_STEPS = 10
STEP_COUNT = fleet_sim.SLOTS_PER_DAY + 1

# ------------------------------------------------------------------ map drawing
FEET_PER_MAP_UNIT = 25.0
SIMPLIFY_TOLERANCE_FEET = 100.0
OUTLINE_GAP_FEET = 30.0

# ------------------------------------------------------------------ numbers quoted from the report
H1_FACTS = {
    "kendall_w_zones": 0.9961, "kendall_w_hours": 0.9755,
    "share_mismatch_zones_percent": 34.07, "share_mismatch_hours_percent": 13.64,
    "weeks": 33, "top10_zone_share_percent": 13.33,
}
FORECAST_METHOD_ORDER = (
    ("Frozen model (HGB)", "Model (gradient boosting)"),
    ("Mean of previous 4 weeks", "4-week mean"),
    ("Last week (lag_7d)", "Last week"),
    ("Frozen 28-day profile (Krish)", "Frozen profile"),
)
DRIFT_METHOD_LABELS = {
    "pred_model": "Model", "pred_four_week_mean": "4-week mean", "pred_last_week": "Last week",
    "pred_frozen_profile": "Frozen profile",
}
SHAP_SHOWN_FEATURES = 8
DATE_FORMAT = "%Y-%m-%d"


# ================================================================== loading

def load_main_test_module():
    """Import notebooks/06_hypothesis_tests.py as a module, so the forecasts are built the same way."""
    spec = importlib.util.spec_from_file_location("main_hypothesis_test", REPO_ROOT / MAIN_TEST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(relative_path):
    """Read one JSON file under the repo root."""
    return json.loads((REPO_ROOT / relative_path).read_text())


def read_zone_table():
    """Read the zones table (names, boroughs, shapes) from the Phase 1 release with duckdb."""
    table_index = read_json(TABLE_INDEX_FILE)
    pattern = (REPO_ROOT / table_index["zones"]["path"] / "**" / "*.parquet").as_posix()
    sql = f"select zone_id, zone_name, borough, geometry_wkb from read_parquet('{pattern}', hive_partitioning=true)"
    return duckdb.connect().execute(sql).df().sort_values("zone_id").reset_index(drop=True)


# ================================================================== map shapes

def ring_to_path(coordinates, min_x, max_y):
    """Turn one ring of (x, y) feet into a compact relative SVG sub-path (whole map units, y flipped)."""
    points = []
    for x_feet, y_feet in coordinates:
        point = (round((x_feet - min_x) / FEET_PER_MAP_UNIT), round((max_y - y_feet) / FEET_PER_MAP_UNIT))
        if not points or point != points[-1]:
            points.append(point)
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    if len(points) < 3:
        return ""
    steps = []
    for (previous_x, previous_y), (x, y) in zip(points, points[1:], strict=False):
        steps.append(f"{x - previous_x} {y - previous_y}")
    return f"M{points[0][0]} {points[0][1]}l{' '.join(steps)}z"


def polygon_to_path(polygon, min_x, max_y):
    """Make the SVG path text of a polygon or multi-polygon (outer rings and holes)."""
    parts = polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]
    paths = []
    for part in parts:
        paths.append(ring_to_path(part.exterior.coords, min_x, max_y))
        for hole in part.interiors:
            paths.append(ring_to_path(hole.coords, min_x, max_y))
    return "".join(paths)


def find_label_point(polygon, min_x, max_y):
    """Where cars sit in a zone: the centroid, or a point inside the shape when the centroid is outside."""
    point = polygon.centroid if polygon.contains(polygon.centroid) else polygon.representative_point()
    return round((point.x - min_x) / FEET_PER_MAP_UNIT, 1), round((max_y - point.y) / FEET_PER_MAP_UNIT, 1)


def build_area_outline(shapes, zone_positions, min_x, max_y):
    """One SVG path around all zones of a launch area (the zones are merged, tiny gaps closed)."""
    merged = unary_union([shapes[position] for position in zone_positions])
    closed = merged.buffer(OUTLINE_GAP_FEET).buffer(-OUTLINE_GAP_FEET)
    return polygon_to_path(closed.simplify(SIMPLIFY_TOLERANCE_FEET, preserve_topology=True), min_x, max_y)


def build_zone_shapes(zone_table, area_positions):
    """Return the map block: simplified SVG paths, car anchor points, names, area outlines and map size.

    area_positions: {area key: list of zone positions (row numbers of the zones table)}.
    """
    shapes = [wkb.loads(bytes(blob)) for blob in zone_table["geometry_wkb"]]
    min_x = min(shape.bounds[0] for shape in shapes)
    max_y = max(shape.bounds[3] for shape in shapes)
    width = round((max(shape.bounds[2] for shape in shapes) - min_x) / FEET_PER_MAP_UNIT)
    height = round((max_y - min(shape.bounds[1] for shape in shapes)) / FEET_PER_MAP_UNIT)
    anchors = [find_label_point(shape, min_x, max_y) for shape in shapes]
    simplified = [shape.simplify(SIMPLIFY_TOLERANCE_FEET, preserve_topology=True) for shape in shapes]
    return {
        "ids": [int(zone_id) for zone_id in zone_table["zone_id"]],
        "names": list(zone_table["zone_name"]),
        "boroughs": list(zone_table["borough"]),
        "paths": [polygon_to_path(shape, min_x, max_y) for shape in simplified],
        "outlines": {key: build_area_outline(shapes, positions, min_x, max_y)
                     for key, positions in area_positions.items()},
        "anchor_x": [anchor[0] for anchor in anchors],
        "anchor_y": [anchor[1] for anchor in anchors],
        "width": width,
        "height": height,
    }


# ================================================================== capturing one simulated run

def capture_run(inputs, requests, forecast, policy, n_cars, zone_ids, settings, seed):
    """Run fleet_sim.simulate_day once while writing down every drive and request.

    Returns (summary row, kept requests, log). The log holds: start (zone position of each
    car before the first request), segments (car, from, to, start minute, end minute, kind) and
    served (numbers of the kept requests that were served).
    """
    log = {"start": None, "segments": [], "served": [], "found_car": None, "pickup_zone": None}
    original = {name: getattr(fleet_sim, name) for name in (
        "find_car_for_request", "serve_request", "drive_car_empty", "apply_moves", "apply_wait_or_move_rule")}

    def remember_start(fleet):
        if log["start"] is None:
            log["start"] = fleet["zone"].copy()

    def find_car_for_request(fleet, is_idle, zone, within_hops, miles_matrix):
        remember_start(fleet)
        car = original["find_car_for_request"](fleet, is_idle, zone, within_hops, miles_matrix)
        log["found_car"] = car
        log["pickup_zone"] = None if car is None else int(fleet["zone"][car])
        return car

    def serve_request(fleet, tally, is_idle, request_number, kept, now_minute, within_hops, inputs_):
        original["serve_request"](fleet, tally, is_idle, request_number, kept, now_minute, within_hops, inputs_)
        car = log["found_car"]
        if car is None:
            return
        origin = int(kept["origin"][request_number])
        pickup_zone = log["pickup_zone"]
        pickup_minutes = inputs_["minutes_matrix"][pickup_zone, origin] if pickup_zone != origin else 0.0
        if pickup_zone != origin:
            log["segments"].append((car, pickup_zone, origin, now_minute, now_minute + pickup_minutes, EMPTY_KIND))
        log["segments"].append((car, origin, int(kept["destination"][request_number]),
                                now_minute + pickup_minutes, float(fleet["free_minute"][car]), LOADED_KIND))
        log["served"].append(request_number)

    def drive_car_empty(fleet, tally, car, to_zone, start_minute, inputs_, is_charged):
        from_zone = int(fleet["zone"][car])
        original["drive_car_empty"](fleet, tally, car, to_zone, start_minute, inputs_, is_charged)
        if is_charged:
            log["segments"].append((int(car), from_zone, int(to_zone), start_minute,
                                    float(fleet["free_minute"][car]), EMPTY_KIND))
        else:
            log["start"][car] = to_zone

    def apply_moves(fleet, tally, moves, now_minute, inputs_, is_charged):
        remember_start(fleet)
        original["apply_moves"](fleet, tally, moves, now_minute, inputs_, is_charged)

    def apply_wait_or_move_rule(fleet, tally, forecast_hour, context, now_minute, inputs_):
        remember_start(fleet)
        original["apply_wait_or_move_rule"](fleet, tally, forecast_hour, context, now_minute, inputs_)

    wrappers = {"find_car_for_request": find_car_for_request, "serve_request": serve_request,
                "drive_car_empty": drive_car_empty, "apply_moves": apply_moves,
                "apply_wait_or_move_rule": apply_wait_or_move_rule}
    for name, wrapper in wrappers.items():
        setattr(fleet_sim, name, wrapper)
    try:
        summary, _ = fleet_sim.simulate_day(inputs, requests, forecast, policy, n_cars, zone_ids, settings, seed)
    finally:
        for name, function in original.items():
            setattr(fleet_sim, name, function)
    is_allowed = fleet_sim.make_allowed_mask(inputs, zone_ids)
    kept = fleet_sim.keep_requests_inside_area(requests, is_allowed)
    return summary, kept, log


# ================================================================== log -> numbers

def count_by_slot(slots, weights=None):
    """Add up weights (or 1 per item) for each of the 96 slots of the day."""
    return np.bincount(np.asarray(slots, dtype=int), weights=weights, minlength=fleet_sim.SLOTS_PER_DAY)


def rebuild_slot_numbers(log, kept, inputs, area_request_slots):
    """From the log alone, work out what happened in each 15-minute slot. Returns a dict of 96-long arrays."""
    served = np.array(log["served"], dtype=int)
    served_slots = kept["slot"][served]
    empty_slots = [int(segment[3] // fleet_sim.SLOT_MINUTES) for segment in log["segments"]
                   if segment[5] == EMPTY_KIND]
    empty_miles = [inputs["miles_matrix"][segment[1], segment[2]] for segment in log["segments"]
                   if segment[5] == EMPTY_KIND]
    served_count = count_by_slot(served_slots)
    return {
        "served": served_count,
        "lost": count_by_slot(area_request_slots) - served_count,
        "passenger_miles": count_by_slot(served_slots, kept["trip_miles"][served]),
        "tolls_usd": count_by_slot(served_slots, kept["toll_usd"][served]),
        "empty_miles": count_by_slot(empty_slots, empty_miles),
    }


def busy_minutes_until(log, minute):
    """Total car-minutes spent driving before a given minute of the day (drives end at midnight at most)."""
    if not log["segments"]:
        return 0.0
    starts = np.array([segment[3] for segment in log["segments"]])
    ends = np.array([segment[4] for segment in log["segments"]])
    limit = min(minute, fleet_sim.DAY_MINUTES)
    return float(np.clip(np.minimum(ends, limit) - starts, 0.0, None).sum())


def build_step_tables(log, kept, inputs, settings, n_cars, area_request_slots):
    """Cumulative scoreboard numbers after 0, 1, ... 96 slots (97 values each), rebuilt from the log.

    Revenue, tolls and miles are booked when the request is served (like the simulator). Idle cost
    is worked out at each quarter hour from the car-minutes spent driving so far.
    """
    per_slot = rebuild_slot_numbers(log, kept, inputs, area_request_slots)
    cumulative = {name: np.concatenate([[0.0], np.cumsum(values)]) for name, values in per_slot.items()}
    minutes = np.arange(STEP_COUNT) * fleet_sim.SLOT_MINUTES
    idle_hours = np.array([(n_cars * minute - busy_minutes_until(log, minute)) / fleet_sim.MINUTES_PER_HOUR
                           for minute in minutes])
    revenue = settings["base_charge_usd"] * cumulative["served"] \
        + settings["per_mile_rate_usd"] * cumulative["passenger_miles"]
    mile_cost = settings["cost_per_mile_usd"] * (cumulative["passenger_miles"] + cumulative["empty_miles"])
    idle_cost = settings["idle_cost_per_car_hour_usd"] * idle_hours
    return {"served": cumulative["served"], "lost": cumulative["lost"], "revenue": revenue,
            "tolls": cumulative["tolls_usd"], "mile_cost": mile_cost, "idle_cost": idle_cost,
            "empty_miles": cumulative["empty_miles"], "passenger_miles": cumulative["passenger_miles"],
            "idle_hours": idle_hours}


def check_log_matches_summary(tables, summary, label):
    """Stop with a clear error unless the numbers rebuilt from the log equal the simulator's own summary."""
    final_profit = (tables["revenue"][-1] - tables["tolls"][-1] - tables["mile_cost"][-1]
                    - tables["idle_cost"][-1])
    all_miles = tables["passenger_miles"][-1] + tables["empty_miles"][-1]
    pairs = {
        "served": (tables["served"][-1], summary["served"]),
        "lost": (tables["lost"][-1], summary["requests_in_area"] - summary["served"]),
        "revenue_usd": (tables["revenue"][-1], summary["revenue_usd"]),
        "tolls_usd": (tables["tolls"][-1], summary["tolls_usd"]),
        "mile_cost_usd": (tables["mile_cost"][-1], summary["mile_cost_usd"]),
        "idle_cost_usd": (tables["idle_cost"][-1], summary["idle_cost_usd"]),
        "empty_miles": (tables["empty_miles"][-1], summary["empty_miles"]),
        "empty_mile_share": (tables["empty_miles"][-1] / all_miles if all_miles else np.nan,
                             summary["empty_mile_share"]),
        "profit_usd": (final_profit, summary["profit_usd"]),
    }
    for name, (from_log, from_simulator) in pairs.items():
        both_missing = np.isnan(from_log) and np.isnan(from_simulator)
        if not both_missing and abs(from_log - from_simulator) > RESULTS_TOLERANCE:
            raise ValueError(f"{label}: {name} from the log is {from_log}, the simulator says {from_simulator}.")
    return pairs


# ================================================================== building the replay block

def round_list(values, digits):
    """A plain list of rounded numbers (whole numbers when digits is 0)."""
    if digits == 0:
        return [int(round(value)) for value in values]
    return [round(float(value), digits) for value in values]


def build_run_block(summary, kept, log, tables, area_request_codes):
    """The data for one (area, plan, fleet) run: starting zones, drive segments, request codes, tables."""
    ordered = sorted(log["segments"], key=lambda segment: (segment[0], segment[3]))
    flat_segments = []
    for car, from_zone, to_zone, start, end, kind in ordered:
        flat_segments += [int(car), int(from_zone), int(to_zone), round(start * MINUTE_STEPS),
                          round(end * MINUTE_STEPS), kind]
    served_set = set(log["served"])
    kept_codes = [CODE_SERVED if number in served_set else CODE_NO_CAR for number in range(len(kept["slot"]))]
    codes = []
    kept_position = 0
    for is_kept in area_request_codes:
        if is_kept:
            codes.append(kept_codes[kept_position])
            kept_position += 1
        else:
            codes.append(CODE_OUTSIDE)
    return {
        "start": [int(zone) for zone in log["start"]],
        "seg": flat_segments,
        "codes": "".join(codes),
        "cum": {"served": round_list(tables["served"], 0), "lost": round_list(tables["lost"], 0),
                "revenue": round_list(tables["revenue"], 2), "tolls": round_list(tables["tolls"], 2),
                "mile_cost": round_list(tables["mile_cost"], 2), "idle_cost": round_list(tables["idle_cost"], 2),
                "empty_miles": round_list(tables["empty_miles"], 2),
                "passenger_miles": round_list(tables["passenger_miles"], 2)},
        "sim": {"requests": int(summary["requests_in_area"]), "served": int(summary["served"]),
                "revenue": round(float(summary["revenue_usd"]), 2), "tolls": round(float(summary["tolls_usd"]), 2),
                "mile_cost": round(float(summary["mile_cost_usd"]), 2),
                "idle_cost": round(float(summary["idle_cost_usd"]), 2),
                "profit": round(float(summary["profit_usd"]), 2),
                "profit_per_car": round(float(summary["profit_per_car_usd"]), 2),
                "empty_share": round(float(summary["empty_mile_share"]), 4),
                "moved_cars": int(summary["moved_cars"])},
    }


def build_area_request_block(requests, is_allowed):
    """Every request that starts inside the area (in time order) and a flag: does the geofence keep it?"""
    starts_inside = is_allowed[requests["origin"]]
    is_kept = starts_inside & is_allowed[requests["destination"]]
    block = {
        "slot": [int(slot) for slot in requests["slot"][starts_inside]],
        "origin": [int(zone) for zone in requests["origin"][starts_inside]],
        "destination": [int(zone) for zone in requests["destination"][starts_inside]],
    }
    return block, [bool(flag) for flag in is_kept[starts_inside]], requests["slot"][starts_inside]


def replay_one_day(setup, area_ids_by_key):
    """Simulate and record every (area, plan, fleet size) run of the demo day. Returns the replay block."""
    inputs, frozen, forecasts, main_test = setup["inputs"], setup["frozen"], setup["forecasts"], setup["main_test"]
    seed = main_test.SIMULATION_SEED
    demand_settings = fleet_sim.make_settings(0.0, 0.0, demand_scale=frozen["demand_scale"])
    requests = fleet_sim.generate_day_requests(inputs, DEMO_DAY, demand_settings, seed)
    replay = {"areas": {}, "runs": {}}
    request_slots = {}
    request_codes = {}
    for key, ids in area_ids_by_key.items():
        is_allowed = fleet_sim.make_allowed_mask(inputs, ids)
        block, kept_flags, slots = build_area_request_block(requests, is_allowed)
        replay["areas"][key] = {"zone_positions": [int(p) for p in np.flatnonzero(is_allowed)], "requests": block}
        request_codes[key], request_slots[key] = kept_flags, slots
    for n_cars in frozen["fleet_sizes"]:
        fleet_choice = frozen["settings_by_fleet_size"][str(n_cars)]
        settings = main_test.make_scenario_settings(setup["price_options"], main_test.MAIN_SCENARIO, fleet_choice)
        for key, ids in area_ids_by_key.items():
            for plan_key, (_, policy, forecast_column, _) in PLANS.items():
                summary, kept, log = capture_run(inputs, requests, forecasts[forecast_column][DEMO_DAY], policy,
                                                 n_cars, ids, settings, seed)
                tables = build_step_tables(log, kept, inputs, settings, n_cars, request_slots[key])
                check_log_matches_summary(tables, summary, f"{key}/{plan_key}/{n_cars}")
                replay["runs"][f"{key}|{plan_key}|{n_cars}"] = build_run_block(
                    summary, kept, log, tables, request_codes[key])
                print(f"  {key:9s} {plan_key:17s} {n_cars:3d} cars: served {summary['served']:5d} of "
                      f"{summary['requests_in_area']:5d}, profit/car {summary['profit_per_car_usd']:8.2f} (log matches)")
    return replay, requests


def check_replay_against_saved_results(replay, daily, h2b_daily):
    """Compare each run's simulator summary with the row saved in the results CSV files for the same day."""
    day_text = DEMO_DAY.strftime(DATE_FORMAT)
    checked = 0
    for run_key, run in replay["runs"].items():
        area_key, plan_key, n_cars = run_key.split("|")
        plan_name = PLANS[plan_key][0]
        if area_key == BASELINE_KEY:
            table, area_label = daily, BASELINE_LABEL
        elif plan_key == "profit":
            table, area_label = h2b_daily, CHOSEN_LABEL
        else:
            continue
        rows = table[(table["scenario"] == "main") & (table["area_label"] == area_label)
                     & (table["plan"] == plan_name) & (table["n_cars"] == int(n_cars)) & (table["date"] == day_text)]
        if len(rows) != 1:
            raise ValueError(f"Expected one saved result row for {run_key}, found {len(rows)}.")
        saved = float(rows.iloc[0]["profit_per_car_usd"])
        if abs(saved - run["sim"]["profit_per_car"]) > 0.01:
            raise ValueError(f"{run_key}: saved profit per car {saved}, replay {run['sim']['profit_per_car']}.")
        checked += 1
    print(f"Replay profit per car equals the saved results CSV rows for {checked} runs.")


# ================================================================== results tables

def read_results_tables():
    """Read the daily results and keep the main scenario on the main test days (price = calibrated fares)."""
    daily = pd.read_csv(REPO_ROOT / DAILY_RESULTS_FILE)
    h2b_daily = pd.read_csv(REPO_ROOT / H2B_DAILY_FILE)
    daily = daily[(daily["scenario"] == "main") & (daily["day_group"] == "main")].copy()
    h2b_daily = h2b_daily[(h2b_daily["scenario"] == "main") & (h2b_daily["day_group"] == "main")].copy()
    return daily, h2b_daily


def label_area(area_label):
    """Short key used by the page for each area label in the results tables."""
    if area_label == BASELINE_LABEL:
        return BASELINE_KEY
    if area_label == CHOSEN_LABEL:
        return CHOSEN_KEY
    return "proxy"


def build_compare_block(daily, h2b_daily):
    """Per-day values for every (area, plan, fleet size) on the 57 main test days, as columns."""
    chosen_rows = h2b_daily[h2b_daily["area_label"] == CHOSEN_LABEL]
    both = pd.concat([daily, chosen_rows], ignore_index=True)
    both["area_key"] = both["area_label"].map(label_area)
    days = sorted(both["date"].unique())
    metric_columns = {"profit": ("profit_per_car_usd", 2), "served": ("served_share", 4),
                      "empty": ("empty_mile_share", 4), "idle": ("idle_car_hours", 1),
                      "gap": ("service_gap", 4), "requests": ("requests_in_area", 0)}
    series = {}
    for (area_key, plan_name, n_cars), rows in both.groupby(["area_key", "plan", "n_cars"]):
        rows = rows.set_index("date").reindex(days)
        if rows["profit_per_car_usd"].isna().any() or len(rows) != len(days):
            raise ValueError(f"Missing days for {area_key}, {plan_name}, {n_cars}.")
        series[f"{area_key}|{plan_name}|{n_cars}"] = {
            name: round_list(rows[column], digits) for name, (column, digits) in metric_columns.items()}
    weekend = both.drop_duplicates("date").set_index("date").reindex(days)["is_weekend"]
    return {"days": days, "is_weekend": [bool(flag) for flag in weekend], "series": series,
            "plans": {name: label for name, _, _, label in PLANS.values()} | {
                "random_allocation": "Random placement", "equal_allocation": "Equal spread",
                "profit_four_week_mean": "Profit placement (4-week mean)",
                "profit_frozen_profile": "Profit placement (frozen profile)"}}


def build_hypothesis_block(daily, h2b_daily):
    """Paired daily differences and test numbers for H2 to H5 and the post hoc H2b."""
    summary = pd.read_csv(REPO_ROOT / HYPOTHESIS_SUMMARY_FILE)
    h2b_summary = pd.read_csv(REPO_ROOT / H2B_TEST_FILE)
    frozen = read_json(FROZEN_SETTINGS_FILE)
    days = sorted(daily["date"].unique())

    def daily_values(table, area_label, plan_name, n_cars):
        rows = table[(table["area_label"] == area_label) & (table["plan"] == plan_name) & (table["n_cars"] == n_cars)]
        return rows.set_index("date")["profit_per_car_usd"].reindex(days).to_numpy()

    pairs = {"H2": ("profit_model", "profit_model"), "H3": ("profit_model", "ride_count_model"),
             "H4": ("profit_model", "profit_last_week"), "H5": ("wait_or_move_model", "stay_model")}
    cards = []
    for _, row in summary[summary["scenario"] == "main"].iterrows():
        if row["hypothesis"] not in pairs or row["family"] not in ("primary_baseline_area", "robustness_chosen_area"):
            continue
        n_cars = int(row["n_cars"])
        chosen_label = frozen["settings_by_fleet_size"][str(n_cars)]["chosen_area_label"]
        first_plan, second_plan = pairs[row["hypothesis"]]
        if row["hypothesis"] == "H2":
            first = daily_values(daily, chosen_label, first_plan, n_cars)
            second = daily_values(daily, BASELINE_LABEL, second_plan, n_cars)
        else:
            area_label = BASELINE_LABEL if row["area_role"] == "baseline" else chosen_label
            first = daily_values(daily, area_label, first_plan, n_cars)
            second = daily_values(daily, area_label, second_plan, n_cars)
        differences = first - second
        append_card(cards, row, differences, row["hypothesis"], row["area_role"], n_cars)
    for _, row in h2b_summary[h2b_summary["scenario"] == "main"].iterrows():
        n_cars = int(row["n_cars"])
        differences = (daily_values(h2b_daily, CHOSEN_LABEL, "profit_model", n_cars)
                       - daily_values(h2b_daily, BASELINE_LABEL, "profit_model", n_cars))
        append_card(cards, row, differences, "H2b", "simulator_chosen", n_cars)
    print(f"Hypothesis cards: {len(cards)} (paired differences match the saved medians).")
    return {"cards": cards, "h1": H1_FACTS}


def append_card(cards, row, differences, hypothesis, area_role, n_cars):
    """Check that the daily differences reproduce the saved median, then store one card row."""
    median = float(np.median(differences))
    if abs(median - float(row["median_difference_usd"])) > 1e-6:
        raise ValueError(f"{hypothesis} {area_role} {n_cars}: median {median} vs saved {row['median_difference_usd']}.")
    cards.append({
        "hypothesis": hypothesis, "area_role": area_role, "n_cars": n_cars, "n_days": int(row["n_days"]),
        "median_diff": round(median, 2), "relative_gain": round(float(row["relative_gain_percent"]), 1),
        "ci_low": round(float(row["ci95_lower_usd"]), 2), "ci_high": round(float(row["ci95_upper_usd"]), 2),
        "p_value": float(row["p_one_sided"]), "verdict": row["verdict"],
        "win_share": round(float(row["win_share"]), 3), "differences": round_list(differences, 2),
    })


def build_forecast_block(day_hourly_requests):
    """Weekly forecast error, drift rules, SHAP bars, the Nov-Dec error table and the demo day's hourly demand."""
    weekly = pd.read_csv(REPO_ROOT / WEEKLY_ERROR_FILE)
    drift = read_json(DRIFT_SUMMARY_FILE)
    forecast = read_json(FORECAST_SUMMARY_FILE)
    model_rows = weekly[weekly["method"] == "pred_model"]
    weeks = list(model_rows["week_start"])
    errors = {}
    for method, label in DRIFT_METHOD_LABELS.items():
        rows = weekly[weekly["method"] == method].set_index("week_start").reindex(weeks)
        errors[label] = {"wape": round_list(rows["WAPE_percent"], 1), "limit": round(float(rows["limit_wape_percent"].iloc[0]), 1),
                         "alert": [bool(flag) for flag in rows["is_alert_week"]]}
    scores = {row["method"]: row for row in forecast["nov_dec_overall_scores"]}
    shap_items = list(forecast["feature_importance_top15"].items())[:SHAP_SHOWN_FEATURES]
    monitor = drift["error_monitor_frozen_model"]
    return {
        "weeks": weeks, "errors": errors, "model_limit": round(monitor["limit_wape_percent"], 1),
        "validation_wape": round(monitor["validation_wape_percent"], 1),
        "first_alert_week": monitor["january_alert_first_week"],
        "psi_alert": drift["rules"]["psi_alert"], "psi_max": round(drift["data_drift_strongest_feature_january"]["psi"], 4),
        "psi_feature": drift["data_drift_strongest_feature_january"]["feature"],
        "shap": [{"feature": name, "value": round(value, 1)} for name, value in shap_items],
        "mae": [{"method": label, "mae": round(float(scores[name]["MAE"]), 2)} for name, label in FORECAST_METHOD_ORDER],
        "day_hour_requests": [int(value) for value in day_hourly_requests],
    }


# ================================================================== output

def collect_everything():
    """Run the whole export. Returns the dict that is written to JSON."""
    main_test = load_main_test_module()
    inputs = fleet_sim.load_simulation_inputs(REPO_ROOT)
    frozen = read_json(FROZEN_SETTINGS_FILE)
    price_options = {"calibrated": frozen["price_calibrated"], "nominal": frozen["price_nominal"]}
    forecasts, _ = main_test.build_forecasts(main_test.read_forecast_table(REPO_ROOT), inputs["zone_ids"])
    h2b_area = read_json(H2B_AREA_FILE)
    area_ids = {BASELINE_KEY: h2b_area["baseline_zone_ids"], CHOSEN_KEY: h2b_area["zone_ids"]}
    if area_ids[BASELINE_KEY] != frozen["baseline_area_zone_ids"]:
        raise ValueError("The baseline area differs between frozen_settings.json and h2b_frozen_area.json.")
    setup = {"inputs": inputs, "frozen": frozen, "forecasts": forecasts, "main_test": main_test,
             "price_options": price_options}
    print(f"Simulating {DEMO_DAY_LABEL}: {len(PLANS)} plans x 2 areas x {len(frozen['fleet_sizes'])} fleet sizes.")
    replay, _ = replay_one_day(setup, area_ids)
    daily, h2b_daily = read_results_tables()
    check_replay_against_saved_results(replay, daily, h2b_daily)
    price = frozen["price_calibrated"]
    day_hours = np.nan_to_num(inputs["hourly_requests"][DEMO_DAY], nan=0.0).sum(axis=1)
    return {
        "meta": {"day": DEMO_DAY.strftime(DATE_FORMAT), "day_label": DEMO_DAY_LABEL, "seed": main_test.SIMULATION_SEED,
                 "demand_scale": frozen["demand_scale"], "fleet_sizes": frozen["fleet_sizes"],
                 "base_charge": round(price["base_charge_usd"], 2), "per_mile": round(price["per_mile_rate_usd"], 2),
                 "cost_per_mile": fleet_sim.COST_PER_MILE_USD, "idle_cost_per_hour": fleet_sim.IDLE_COST_PER_CAR_HOUR_USD,
                 "slot_minutes": fleet_sim.SLOT_MINUTES, "minute_steps": MINUTE_STEPS,
                 "area_sizes": {key: len(ids) for key, ids in area_ids.items()},
                 "settings_by_fleet": {size: {"requests_per_car_hour": frozen["settings_by_fleet_size"][str(size)]["requests_per_car_hour"],
                                              "move_ratio": frozen["settings_by_fleet_size"][str(size)]["move_ratio"]}
                                       for size in frozen["fleet_sizes"]},
                 "plans": {key: label for key, (_, _, _, label) in PLANS.items()}},
        "zones": build_zone_shapes(read_zone_table(), {key: replay["areas"][key]["zone_positions"] for key in area_ids}),
        "replay": replay,
        "compare": build_compare_block(daily, h2b_daily),
        "hypotheses": build_hypothesis_block(daily, h2b_daily),
        "forecast": build_forecast_block(day_hours),
    }


def write_outputs(everything):
    """Write the JSON file and the final HTML page (template with the JSON put inside)."""
    json_text = json.dumps(everything, separators=(",", ":"), allow_nan=False)
    json_path = REPO_ROOT / JSON_FILE
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json_text)
    template = (REPO_ROOT / TEMPLATE_FILE).read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise ValueError(f"{TEMPLATE_FILE} must contain {PLACEHOLDER} exactly once.")
    safe_json = json_text.replace("</", "<\\/")
    html_path = REPO_ROOT / HTML_FILE
    html_path.write_text(template.replace(PLACEHOLDER, safe_json), encoding="utf-8")
    print(f"Wrote {json_path} ({len(json_text) / 1e6:.2f} MB) and {html_path} ({html_path.stat().st_size / 1e6:.2f} MB).")


def main():
    """Export everything and write the page."""
    write_outputs(collect_everything())


if __name__ == "__main__":
    main()
