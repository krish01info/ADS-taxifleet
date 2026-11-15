"""Check the Phase 4 fleet simulator on a tiny invented city of three zones in a row."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src import fleet_sim as fs

TEST_DAY = dt.date(2024, 11, 4)
ZONE_IDS = np.array([10, 20, 30])
BASE_CHARGE_USD = 5.0
PER_MILE_RATE_USD = 2.0
ZONE_0_TOLL_USD = 0.5
TRIP_MILES_BETWEEN_NEIGHBOURS = 3.0
TRIP_MINUTES_BETWEEN_NEIGHBOURS = 9.0
FULL_DEMAND_SCALE = 1.0
HOURS_IN_DAY = 24


def make_toy_inputs(hourly_actual):
    """Three zones in a row (0-1-2). Zone 0 trips go to 1, zone 1 trips to 0, zone 2 trips to 1."""
    miles_matrix = np.array([[1.0, 3.0, 6.0], [3.0, 1.0, 3.0], [6.0, 3.0, 1.0]])
    adjacent = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], dtype=bool)
    destination_cdf = np.array([[0.0, 1.0, 1.0], [1.0, 1.0, 1.0], [0.0, 1.0, 1.0]])
    return {
        "zone_ids": ZONE_IDS,
        "miles_matrix": miles_matrix,
        "minutes_matrix": miles_matrix * TRIP_MINUTES_BETWEEN_NEIGHBOURS / TRIP_MILES_BETWEEN_NEIGHBOURS,
        "destination_cdf": destination_cdf,
        "adjacent": adjacent,
        "mean_toll_usd": np.array([ZONE_0_TOLL_USD, 0.0, 0.0]),
        "hourly_requests": {TEST_DAY: hourly_actual},
    }


def make_toy_settings(**overrides):
    """Settings with full demand (no thinning) and easy round prices."""
    settings = {"demand_scale": FULL_DEMAND_SCALE, "cost_per_mile_usd": 1.0, "idle_cost_per_car_hour_usd": 2.0}
    settings.update(overrides)
    return fs.make_settings(BASE_CHARGE_USD, PER_MILE_RATE_USD, **settings)


def make_actual(requests_by_hour_and_zone):
    """Build a (24, 3) demand array from {(hour, zone_position): count}."""
    actual = np.zeros((HOURS_IN_DAY, len(ZONE_IDS)))
    for (hour, zone_position), count in requests_by_hour_and_zone.items():
        actual[hour, zone_position] = count
    return actual


def make_forecast(first_hour_zone_position, later_zone_position):
    """A forecast that puts all demand in one zone at hour 0 and in another zone afterwards."""
    forecast = np.zeros((HOURS_IN_DAY, len(ZONE_IDS)))
    forecast[0, first_hour_zone_position] = 1.0
    forecast[1:, later_zone_position] = 1.0
    return forecast


def run_one_policy(inputs, forecast, policy, n_cars, settings, allowed_zone_ids=ZONE_IDS, seed=1):
    """Generate the day's requests and simulate one policy. Returns (summary, zone_table)."""
    requests = fs.generate_day_requests(inputs, TEST_DAY, settings, seed)
    return fs.simulate_day(inputs, requests, forecast, policy, n_cars, allowed_zone_ids, settings, seed)


def test_one_car_one_request_matches_hand_calculation():
    inputs = make_toy_inputs(make_actual({(10, 0): 1}))
    summary, _ = run_one_policy(inputs, make_forecast(0, 0), "stay", 1, make_toy_settings())
    trip_miles = TRIP_MILES_BETWEEN_NEIGHBOURS
    idle_car_hours = (fs.DAY_MINUTES - TRIP_MINUTES_BETWEEN_NEIGHBOURS) / fs.MINUTES_PER_HOUR
    assert summary["served"] == 1
    assert summary["revenue_usd"] == pytest.approx(BASE_CHARGE_USD + PER_MILE_RATE_USD * trip_miles)
    assert summary["tolls_usd"] == pytest.approx(ZONE_0_TOLL_USD)
    assert summary["mile_cost_usd"] == pytest.approx(1.0 * trip_miles)
    assert summary["idle_car_hours"] == pytest.approx(idle_car_hours)
    assert summary["idle_cost_usd"] == pytest.approx(2.0 * idle_car_hours)
    assert summary["profit_usd"] == pytest.approx(11.0 - 0.5 - 3.0 - 2.0 * idle_car_hours)


def test_request_is_unserved_when_no_car_is_within_reach():
    inputs = make_toy_inputs(make_actual({(10, 2): 1}))
    summary, _ = run_one_policy(inputs, make_forecast(0, 0), "stay", 1, make_toy_settings())
    assert summary["requests_in_area"] == 1
    assert summary["served"] == 0
    assert summary["revenue_usd"] == 0.0


def test_neighbour_car_serves_request_and_empty_pickup_miles_are_charged():
    inputs = make_toy_inputs(make_actual({(10, 1): 1}))
    summary, _ = run_one_policy(inputs, make_forecast(0, 0), "stay", 1, make_toy_settings())
    assert summary["served"] == 1
    assert summary["empty_miles"] == pytest.approx(TRIP_MILES_BETWEEN_NEIGHBOURS)
    assert summary["mile_cost_usd"] == pytest.approx(2 * TRIP_MILES_BETWEEN_NEIGHBOURS)
    busy_hours = 2 * TRIP_MINUTES_BETWEEN_NEIGHBOURS / fs.MINUTES_PER_HOUR
    assert summary["idle_car_hours"] == pytest.approx(HOURS_IN_DAY - busy_hours)


def test_forced_reposition_charges_empty_miles_and_busy_time():
    inputs = make_toy_inputs(make_actual({}))
    summary, _ = run_one_policy(inputs, make_forecast(0, 1), "ride_count", 1, make_toy_settings())
    assert summary["moved_cars"] == 1
    assert summary["empty_miles"] == pytest.approx(TRIP_MILES_BETWEEN_NEIGHBOURS)
    assert summary["empty_mile_share"] == pytest.approx(1.0)
    assert summary["mile_cost_usd"] == pytest.approx(TRIP_MILES_BETWEEN_NEIGHBOURS)
    busy_hours = TRIP_MINUTES_BETWEEN_NEIGHBOURS / fs.MINUTES_PER_HOUR
    assert summary["idle_car_hours"] == pytest.approx(HOURS_IN_DAY - busy_hours)


def test_stay_policy_never_repositions_cars():
    inputs = make_toy_inputs(make_actual({(hour, 0): 3 for hour in range(HOURS_IN_DAY)}))
    summary, _ = run_one_policy(inputs, make_forecast(0, 1), "stay", 2, make_toy_settings())
    assert summary["moved_cars"] == 0
    assert summary["served"] > 0


def test_wait_or_move_moves_only_when_own_zone_is_much_quieter():
    inputs = make_toy_inputs(make_actual({(0, 0): 1}))
    forecast = np.zeros((HOURS_IN_DAY, len(ZONE_IDS)))
    forecast[:, 0] = 1.0
    forecast[:, 1] = 0.1
    moving, _ = run_one_policy(inputs, forecast, "wait_or_move", 1, make_toy_settings())
    waiting, _ = run_one_policy(inputs, forecast, "stay", 1, make_toy_settings())
    assert moving["moved_cars"] == 1
    assert moving["empty_miles"] == pytest.approx(TRIP_MILES_BETWEEN_NEIGHBOURS)
    assert waiting["moved_cars"] == 0


def test_cars_are_conserved_and_stay_inside_the_allowed_zones():
    inputs = make_toy_inputs(make_actual({(hour, zone): 4 for hour in range(HOURS_IN_DAY) for zone in range(2)}))
    settings = make_toy_settings()
    requests = fs.generate_day_requests(inputs, TEST_DAY, settings, 1)
    is_allowed = fs.make_allowed_mask(inputs, ZONE_IDS[:2])
    for policy in fs.POLICIES:
        fleet, tally, kept = fs.run_day(inputs, requests, make_forecast(0, 1), policy, 5, is_allowed, settings, 1)
        assert len(fleet["zone"]) == 5
        assert is_allowed[fleet["zone"]].all()
        assert np.bincount(fleet["zone"], minlength=len(ZONE_IDS)).sum() == 5
        assert tally["served"].sum() <= len(kept["origin"])


def test_requests_ending_outside_the_area_are_dropped_and_counted():
    inputs = make_toy_inputs(make_actual({(10, 2): 2}))
    summary, _ = run_one_policy(
        inputs, make_forecast(1, 1), "stay", 1, make_toy_settings(), allowed_zone_ids=ZONE_IDS[:2]
    )
    assert summary["requests_all_zones"] == 2
    assert summary["requests_in_area"] == 0
    assert summary["served"] == 0


def test_same_seed_gives_identical_tables_and_other_seed_differs():
    inputs = make_toy_inputs(make_actual({(hour, zone): 5 for hour in range(HOURS_IN_DAY) for zone in range(3)}))
    settings = fs.make_settings(BASE_CHARGE_USD, PER_MILE_RATE_USD, demand_scale=0.5)
    forecast_by_date = {TEST_DAY: make_forecast(0, 1)}
    args = (inputs, [TEST_DAY], forecast_by_date, fs.POLICIES, 4, ZONE_IDS, settings)
    first = fs.simulate_days(*args, seed=7)
    second = fs.simulate_days(*args, seed=7)
    other = fs.simulate_days(*args, seed=8)
    pd.testing.assert_frame_equal(first, second)
    assert not first["requests_all_zones"].equals(other["requests_all_zones"])


def test_every_policy_sees_the_same_realised_demand():
    inputs = make_toy_inputs(make_actual({(hour, zone): 5 for hour in range(HOURS_IN_DAY) for zone in range(3)}))
    settings = fs.make_settings(BASE_CHARGE_USD, PER_MILE_RATE_USD, demand_scale=0.5)
    table = fs.simulate_days(inputs, [TEST_DAY], {TEST_DAY: make_forecast(0, 1)}, fs.POLICIES, 4, ZONE_IDS, settings)
    assert len(table) == len(fs.POLICIES)
    assert table["requests_all_zones"].nunique() == 1
    assert table["requests_in_area"].nunique() == 1


def test_idle_cost_follows_idle_car_hours():
    inputs = make_toy_inputs(make_actual({(10, 0): 2}))
    forecast = make_forecast(0, 0)
    free_idle, _ = run_one_policy(inputs, forecast, "stay", 2, make_toy_settings(idle_cost_per_car_hour_usd=0.0))
    dear_idle, _ = run_one_policy(inputs, forecast, "stay", 2, make_toy_settings(idle_cost_per_car_hour_usd=5.0))
    assert free_idle["idle_cost_usd"] == 0.0
    assert dear_idle["idle_cost_usd"] == pytest.approx(5.0 * dear_idle["idle_car_hours"])
    assert dear_idle["profit_usd"] == pytest.approx(free_idle["profit_usd"] - dear_idle["idle_cost_usd"])


def test_missing_actual_hours_are_counted_and_treated_as_zero_requests():
    actual = make_actual({(10, 0): 1})
    actual[3, 0] = np.nan
    inputs = make_toy_inputs(actual)
    summary, _ = run_one_policy(inputs, make_forecast(0, 0), "stay", 1, make_toy_settings())
    assert summary["missing_zone_hours_in_area"] == 1
    assert summary["requests_all_zones"] == 1


def test_fit_common_price_recovers_known_price():
    rng = np.random.default_rng(0)
    trips = rng.integers(1, 50, 200)
    miles = trips * rng.uniform(2, 8, 200)
    table = pd.DataFrame({
        "valid_fare_count": trips,
        "passenger_miles_sum": miles,
        "valid_base_fare_sum_usd": 4.0 * trips + 1.5 * miles,
    })
    price = fs.fit_common_price(table)
    assert price["base_charge_usd"] == pytest.approx(4.0)
    assert price["per_mile_rate_usd"] == pytest.approx(1.5)


def test_split_cars_by_weights_keeps_every_car():
    cars = fs.split_cars_by_weights(7, [1.0, 1.0, 1.0])
    assert cars.sum() == 7
    assert fs.split_cars_by_weights(3, [0.0, 0.0]).sum() == 3


def test_within_hops_matrix_grows_with_hops():
    adjacent = make_toy_inputs(make_actual({}))["adjacent"]
    one_hop = fs.build_within_hops_matrix(adjacent, 1)
    two_hops = fs.build_within_hops_matrix(adjacent, 2)
    assert not one_hop[0, 2]
    assert two_hops[0, 2]
    assert not two_hops[0, 0]


def test_unknown_policy_and_bad_forecast_fail_loudly():
    inputs = make_toy_inputs(make_actual({}))
    with pytest.raises(ValueError):
        run_one_policy(inputs, make_forecast(0, 0), "teleport", 1, make_toy_settings())
    with pytest.raises(ValueError):
        run_one_policy(inputs, np.zeros((3, 3)), "stay", 1, make_toy_settings())
