"""Check the demo export (notebooks/08_export_demo_data.py) on a tiny invented city of three zones."""

import importlib.util
from datetime import date
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box

from src import fleet_sim

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "08_export_demo_data.py"
TEST_DAY = date(2024, 11, 12)
ZONE_IDS = np.array([10, 20, 30])
TOY_SEED = 5
TOY_CAR_COUNT = 3
TOY_BASE_CHARGE_USD = 5.0
TOY_PER_MILE_USD = 2.0
TOY_REQUESTS_BY_HOUR_AND_ZONE = {(10, 0): 8, (10, 1): 6, (10, 2): 4, (11, 0): 5, (15, 2): 7, (0, 1): 3}
TOY_POLICIES = ("stay", "ride_count", "profit", "wait_or_move", "random", "equal")


def load_export_module():
    """Import the export script as a module (its file name starts with a number)."""
    spec = importlib.util.spec_from_file_location("export_demo_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export = load_export_module()


def make_toy_setup():
    """Three zones in a row (0-1-2), real demand of a few requests per hour, full demand scale."""
    miles_matrix = np.array([[1.0, 3.0, 6.0], [3.0, 1.0, 3.0], [6.0, 3.0, 1.0]])
    actual = np.zeros((fleet_sim.HOURS_PER_DAY, len(ZONE_IDS)))
    for (hour, zone_position), count in TOY_REQUESTS_BY_HOUR_AND_ZONE.items():
        actual[hour, zone_position] = count
    inputs = {
        "zone_ids": ZONE_IDS,
        "miles_matrix": miles_matrix,
        "minutes_matrix": miles_matrix * 3.0,
        "destination_cdf": np.array([[0.0, 0.5, 1.0], [0.5, 0.5, 1.0], [0.0, 1.0, 1.0]]),
        "adjacent": np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], dtype=bool),
        "mean_toll_usd": np.array([0.5, 0.0, 0.25]),
        "hourly_requests": {TEST_DAY: actual},
    }
    settings = fleet_sim.make_settings(
        TOY_BASE_CHARGE_USD, TOY_PER_MILE_USD, demand_scale=1.0, cost_per_mile_usd=1.0,
        idle_cost_per_car_hour_usd=2.0, requests_per_car_hour=2.0, move_ratio=0.5)
    requests = fleet_sim.generate_day_requests(inputs, TEST_DAY, settings, TOY_SEED)
    forecast = np.ones((fleet_sim.HOURS_PER_DAY, len(ZONE_IDS)))
    forecast[:, 2] = 3.0
    return inputs, settings, requests, forecast


def test_polygon_path_is_closed_and_starts_at_the_scaled_corner():
    square = box(0.0, 0.0, 250.0, 250.0)
    path = export.polygon_to_path(square, 0.0, 250.0)
    assert path.startswith("M")
    assert path.endswith("z")
    steps_per_side = 250.0 / export.FEET_PER_MAP_UNIT
    assert f"{steps_per_side:.0f}" in path


@pytest.mark.parametrize("policy", TOY_POLICIES)
def test_log_gives_back_the_simulator_summary(policy):
    inputs, settings, requests, forecast = make_toy_setup()
    summary, kept, log = export.capture_run(inputs, requests, forecast, policy, TOY_CAR_COUNT, ZONE_IDS, settings, TOY_SEED)
    is_allowed = fleet_sim.make_allowed_mask(inputs, ZONE_IDS)
    slots = requests["slot"][is_allowed[requests["origin"]]]
    tables = export.build_step_tables(log, kept, inputs, settings, TOY_CAR_COUNT, slots)
    export.check_log_matches_summary(tables, summary, policy)
    assert len(log["start"]) == TOY_CAR_COUNT
    assert tables["served"][-1] == summary["served"]
    assert summary["served"] > 0


def test_geofence_drops_are_counted_as_lost_and_helpers_are_restored():
    inputs, settings, requests, forecast = make_toy_setup()
    original_serve = fleet_sim.serve_request
    allowed_ids = ZONE_IDS[:2]
    summary, kept, log = export.capture_run(inputs, requests, forecast, "profit", TOY_CAR_COUNT, allowed_ids, settings, TOY_SEED)
    assert fleet_sim.serve_request is original_serve
    is_allowed = fleet_sim.make_allowed_mask(inputs, allowed_ids)
    block, kept_flags, slots = export.build_area_request_block(requests, is_allowed)
    assert len(block["slot"]) == summary["requests_in_area"]
    assert sum(kept_flags) == len(kept["slot"])
    tables = export.build_step_tables(log, kept, inputs, settings, TOY_CAR_COUNT, slots)
    assert tables["lost"][-1] == summary["requests_in_area"] - summary["served"]
    assert tables["lost"][-1] >= summary["requests_dropped_outside"]
