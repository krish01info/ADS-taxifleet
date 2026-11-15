"""Check the H2b helpers (notebooks/06b_h2_simulated_area.py) on small invented data, no real data."""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import fleet_sim, launch_area

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "06b_h2_simulated_area.py"
TOY_SEED = 3
TOY_DAY_COUNT = 20
TOY_FLEET_SIZES = (25, 50, 100)


def load_h2b_module():
    """Import the H2b script as a module (its file name starts with a number, so no normal import)."""
    spec = importlib.util.spec_from_file_location("h2b_script", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


h2b = load_h2b_module()


def make_toy_requests():
    """Four requests: origins 0, 0, 1, 2 and destinations 0, 2, 1, 0."""
    return {"slot": np.array([0, 1, 2, 3]), "origin": np.array([0, 0, 1, 2]),
            "destination": np.array([0, 2, 1, 0]), "trip_miles": np.ones(4),
            "trip_minutes": np.ones(4), "toll_usd": np.zeros(4)}


def test_pickup_only_geofence_keeps_trips_that_end_outside():
    is_allowed = np.array([True, True, False])
    requests = make_toy_requests()
    both_ends = fleet_sim.keep_requests_inside_area(requests, is_allowed)
    pickup_only = h2b.keep_requests_starting_inside_area(requests, is_allowed)
    assert len(both_ends["origin"]) == 2
    assert len(pickup_only["origin"]) == 3


def test_switching_the_destination_geofence_off_is_undone_afterwards():
    original = fleet_sim.keep_requests_inside_area
    with h2b.destination_geofence_switched_off():
        assert fleet_sim.keep_requests_inside_area is h2b.keep_requests_starting_inside_area
    assert fleet_sim.keep_requests_inside_area is original
    with pytest.raises(RuntimeError), h2b.destination_geofence_switched_off():
        raise RuntimeError("stop")
    assert fleet_sim.keep_requests_inside_area is original


def test_sampled_moves_are_connected_and_inside_the_size_limits(monkeypatch):
    monkeypatch.setattr(h2b, "MIN_ZONES", 2)
    monkeypatch.setattr(h2b, "MAX_ZONES", 4)
    monkeypatch.setattr(h2b, "MIN_TRAINING_DEMAND_SHARE", 0.0)
    graph = {1: {2}, 2: {1, 3}, 3: {2, 4}, 4: {3, 5}, 5: {4, 6}, 6: {5}}
    setup = {"graph": graph, "zone_demand_share": {zone_id: 1 / 6 for zone_id in graph}}
    rng = np.random.default_rng(TOY_SEED)
    moves = h2b.sample_allowed_moves(setup, {2, 3, 4}, rng, 50)
    assert len(moves) > 0
    for move in moves:
        assert 2 <= len(move) <= 4
        assert launch_area.area_is_connected(graph, move)


def make_toy_daily(chosen_extra_usd):
    """A daily table where the chosen area earns chosen_extra_usd more than the baseline, plus noise."""
    rng = np.random.default_rng(TOY_SEED)
    rows = []
    for n_cars in TOY_FLEET_SIZES:
        for day_number in range(TOY_DAY_COUNT):
            baseline_profit = 200.0 + rng.normal(0, 5)
            for area_label, profit in ((h2b.BASELINE_LABEL, baseline_profit),
                                       (h2b.CHOSEN_LABEL, baseline_profit + chosen_extra_usd + rng.normal(0, 1))):
                rows.append({"scenario": h2b.SCENARIO_GEOFENCE_ON, "n_cars": n_cars, "area_label": area_label,
                             "plan": h2b.PLAN_NAME, "date": date(2024, 11, 1) + timedelta(days=day_number),
                             "profit_per_car_usd": profit})
    return pd.DataFrame(rows)


def test_verdict_is_supported_when_chosen_area_clearly_earns_more():
    daily = make_toy_daily(chosen_extra_usd=20.0)
    days = sorted(daily["date"].unique())
    table = h2b.compare_areas(daily, h2b.SCENARIO_GEOFENCE_ON, TOY_FLEET_SIZES, days)
    assert list(table["n_cars"]) == list(TOY_FLEET_SIZES)
    assert (table["median_difference_usd"] > 0).all()
    assert (table["fleet_size_status"] == h2b.ht.STATUS_PASS).all()
    assert (table["verdict"] == "supported").all()


def test_verdict_is_not_supported_when_chosen_area_clearly_earns_less():
    daily = make_toy_daily(chosen_extra_usd=-20.0)
    days = sorted(daily["date"].unique())
    table = h2b.compare_areas(daily, h2b.SCENARIO_GEOFENCE_ON, TOY_FLEET_SIZES, days)
    assert (table["verdict"] == "not supported").all()
    assert (table["p_one_sided"] > 0.9).all()


def test_geofence_summary_pools_requests_over_days():
    days = [date(2024, 11, 1), date(2024, 11, 2)]
    daily = pd.DataFrame({
        "scenario": "main", "n_cars": 25, "area_label": "a", "date": days,
        "requests_in_area": [100, 300], "requests_all_zones": [200, 600], "requests_dropped_outside": [10, 30],
        "served": [40, 120], "utilisation": [0.5, 0.5], "empty_mile_share": [0.1, 0.1],
        "profit_per_car_usd": [1.0, 3.0]})
    summary = h2b.summarise_geofence(daily, days)
    assert summary["dropped_outside_share_of_area_requests"].iloc[0] == pytest.approx(0.1)
    assert summary["served_share_of_area_requests"].iloc[0] == pytest.approx(0.4)
    assert summary["share_of_all_city_requests_starting_in_area"].iloc[0] == pytest.approx(0.5)
    assert summary["median_profit_per_car_usd"].iloc[0] == pytest.approx(2.0)
