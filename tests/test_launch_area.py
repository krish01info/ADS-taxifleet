"""Check the Phase 5 launch-area code on small invented zones and graphs (no real data)."""

import math

import numpy as np
import pandas as pd
import pytest

from src import launch_area
from src.launch_area import (
    BRIDGE_LINK_COLUMNS,
    BRIDGE_LINKS,
    LINK_TYPE_AIRPORT_ROAD,
    LINK_TYPE_RIVER_CROSSING,
    area_is_connected,
    build_proxy_inputs,
    build_zone_graph,
    count_new_edges,
    find_best_area_by_brute_force,
    find_connected_groups,
    fixed_baseline_area,
    grow_launch_area,
    make_proxy_score,
    pick_top_demand_seeds,
    select_bridge_links,
)

TOY_SEED = 7


def make_toy_zones(zone_ids):
    """A zones table with only the columns the graph code needs."""
    return pd.DataFrame({"zone_id": zone_ids})


def make_neighbour_table(pairs):
    """A polygon neighbour table with both directions of every pair, like the real one."""
    rows = []
    for zone_id, other_zone_id in pairs:
        rows.append({"zone_id": zone_id, "neighbour_zone_id": other_zone_id})
        rows.append({"zone_id": other_zone_id, "neighbour_zone_id": zone_id})
    return pd.DataFrame(rows)


def make_random_toy_graph(random_generator, zone_count):
    """A random connected graph: a chain of zones plus a few extra links."""
    pairs = [(zone_id, zone_id + 1) for zone_id in range(1, zone_count)]
    for _ in range(zone_count // 2):
        first, second = random_generator.choice(np.arange(1, zone_count + 1), 2, replace=False)
        pairs.append((int(first), int(second)))
    zones = make_toy_zones(range(1, zone_count + 1))
    return build_zone_graph(zones, make_neighbour_table(pairs))


def make_toy_score_function(random_generator, zone_count):
    """Zone values (some negative) plus a bonus for each pair of zones with a shared border."""
    values = {zone_id: float(random_generator.normal(1.0, 2.0))
              for zone_id in range(1, zone_count + 1)}

    def score_function(area_zone_ids):
        return sum(values[zone_id] for zone_id in area_zone_ids) - 0.1 * len(area_zone_ids) ** 2

    return score_function


# ------------------------------------------------------------------ zone graph

def test_graph_is_symmetric_and_has_no_self_links():
    zones = make_toy_zones([1, 2, 3, 4])
    polygon_neighbours = pd.DataFrame({"zone_id": [1, 2], "neighbour_zone_id": [2, 3]})
    graph = build_zone_graph(zones, polygon_neighbours, bridge_links=[(3, 4)])
    for zone_id, neighbours in graph.items():
        assert zone_id not in neighbours
        for neighbour_id in neighbours:
            assert zone_id in graph[neighbour_id]
    assert graph[4] == {3}


def test_bridge_links_connect_two_islands():
    zones = make_toy_zones([1, 2, 3, 4])
    polygon_neighbours = make_neighbour_table([(1, 2), (3, 4)])
    without_bridge = build_zone_graph(zones, polygon_neighbours)
    with_bridge = build_zone_graph(zones, polygon_neighbours, bridge_links=[(2, 3, "Bridge")])
    assert len(find_connected_groups(without_bridge)) == 2
    assert len(find_connected_groups(with_bridge)) == 1
    assert count_new_edges(without_bridge, with_bridge) == 1


def test_graph_rejects_unknown_zone_and_self_link():
    zones = make_toy_zones([1, 2])
    polygon_neighbours = make_neighbour_table([(1, 2)])
    with pytest.raises(ValueError):
        build_zone_graph(zones, polygon_neighbours, bridge_links=[(2, 99)])
    with pytest.raises(ValueError):
        build_zone_graph(zones, polygon_neighbours, bridge_links=[(2, 2)])


def test_connected_groups_are_listed_biggest_first():
    graph = {1: {2}, 2: {1}, 3: set(), 4: {5}, 5: {4, 6}, 6: {5}}
    groups = find_connected_groups(graph)
    assert [len(group) for group in groups] == [3, 2, 1]


def test_area_is_connected():
    graph = {1: {2}, 2: {1, 3}, 3: {2}, 4: set()}
    assert area_is_connected(graph, [1, 2, 3])
    assert not area_is_connected(graph, [1, 3])
    assert not area_is_connected(graph, [1, 4])
    assert not area_is_connected(graph, [])


def test_bridge_link_list_is_modest_unique_and_documented():
    pairs = [frozenset(link[:2]) for link in BRIDGE_LINKS]
    assert 15 <= len(BRIDGE_LINKS) <= 40
    assert len(set(pairs)) == len(pairs)
    for link in BRIDGE_LINKS:
        assert len(link) == len(BRIDGE_LINK_COLUMNS)
        assert link[0] != link[1]
        assert link[3] in (LINK_TYPE_RIVER_CROSSING, LINK_TYPE_AIRPORT_ROAD)
        assert link[4]


def test_select_bridge_links_filters_by_type():
    river_only = select_bridge_links([LINK_TYPE_RIVER_CROSSING])
    assert 0 < len(river_only) < len(BRIDGE_LINKS)
    assert all(link[3] == LINK_TYPE_RIVER_CROSSING for link in river_only)


# --------------------------------------------------------------- baseline area

def make_baseline_toy_zones():
    """Manhattan zones 1-3 (3 is a ferry-only island), Brooklyn zones 10-12, Bronx zone 20."""
    names = {1: "Midtown", 2: "SoHo", 3: "Ferry Island", 10: "Downtown Brooklyn/MetroTech",
             11: "Brooklyn Heights", 12: "Coney Island", 20: "Fordham"}
    boroughs = {1: "Manhattan", 2: "Manhattan", 3: "Manhattan", 10: "Brooklyn",
                11: "Brooklyn", 12: "Brooklyn", 20: "Bronx"}
    return pd.DataFrame({"zone_id": list(names), "zone_name": list(names.values()),
                         "borough": [boroughs[zone_id] for zone_id in names]})


def test_baseline_is_manhattan_plus_named_brooklyn_zones(monkeypatch):
    monkeypatch.setattr(launch_area, "DOWNTOWN_BROOKLYN_ZONE_NAMES",
                        ("Downtown Brooklyn/MetroTech", "Brooklyn Heights"))
    monkeypatch.setattr(launch_area, "FERRY_ONLY_ZONE_IDS", (3,))
    assert fixed_baseline_area(make_baseline_toy_zones()) == [1, 2, 10, 11]


def test_baseline_stops_when_a_brooklyn_name_is_missing(monkeypatch):
    monkeypatch.setattr(launch_area, "DOWNTOWN_BROOKLYN_ZONE_NAMES",
                        ("Downtown Brooklyn/MetroTech", "Not A Real Zone"))
    with pytest.raises(ValueError):
        fixed_baseline_area(make_baseline_toy_zones())


# ------------------------------------------------------------------- optimiser

def test_optimiser_returns_connected_area_inside_size_limits():
    random_generator = np.random.default_rng(TOY_SEED)
    graph = make_random_toy_graph(random_generator, 12)
    score_function = make_toy_score_function(random_generator, 12)
    result = grow_launch_area(graph, score_function, min_zones=3, max_zones=6)
    assert area_is_connected(graph, result["zone_ids"])
    assert 3 <= len(result["zone_ids"]) <= 6
    assert result["score"] == pytest.approx(score_function(result["zone_ids"]))


@pytest.mark.parametrize("toy_seed", range(TOY_SEED, TOY_SEED + 6))
def test_optimiser_finds_the_brute_force_optimum_on_toy_graphs(toy_seed):
    random_generator = np.random.default_rng(toy_seed)
    graph = make_random_toy_graph(random_generator, 9)
    score_function = make_toy_score_function(random_generator, 9)
    best_by_search = grow_launch_area(graph, score_function, min_zones=2, max_zones=6)
    best_by_brute_force = find_best_area_by_brute_force(graph, score_function, 2, 6)
    assert area_is_connected(graph, best_by_search["zone_ids"])
    assert best_by_search["score"] == pytest.approx(best_by_brute_force["score"])


def test_optimiser_does_not_join_disconnected_high_value_zones():
    graph = {1: {2}, 2: {1, 3}, 3: {2, 4}, 4: {3, 5}, 5: {4}}
    values = {1: 10.0, 2: -5.0, 3: -5.0, 4: -5.0, 5: 10.0}

    def score_function(area_zone_ids):
        return sum(values[zone_id] for zone_id in area_zone_ids)

    result = grow_launch_area(graph, score_function, min_zones=1, max_zones=5)
    assert area_is_connected(graph, result["zone_ids"])
    assert result["zone_ids"] in ([1], [5])


def test_minimum_size_is_respected():
    graph = {1: {2}, 2: {1, 3}, 3: {2}}
    values = {1: 5.0, 2: -1.0, 3: -1.0}

    def score_function(area_zone_ids):
        return sum(values[zone_id] for zone_id in area_zone_ids)

    result = grow_launch_area(graph, score_function, min_zones=2, max_zones=3)
    assert len(result["zone_ids"]) == 2
    assert result["zone_ids"] == [1, 2]


def test_minimum_demand_share_is_respected():
    graph = {1: {2}, 2: {1, 3}, 3: {2}}
    values = {1: 5.0, 2: -1.0, 3: -1.0}
    demand_share = {1: 0.2, 2: 0.3, 3: 0.5}

    def score_function(area_zone_ids):
        return sum(values[zone_id] for zone_id in area_zone_ids)

    result = grow_launch_area(graph, score_function, min_zones=1, max_zones=3,
                              zone_demand_share=demand_share, min_demand_share=0.5)
    assert result["demand_share"] >= 0.5
    assert result["zone_ids"] == [1, 2]


def test_brute_force_also_respects_the_demand_share():
    graph = {1: {2}, 2: {1, 3}, 3: {2}}
    demand_share = {1: 0.2, 2: 0.3, 3: 0.5}
    best = find_best_area_by_brute_force(
        graph, lambda area_zone_ids: -len(area_zone_ids), 1, 3, demand_share, 0.5)
    assert best["zone_ids"] == [3]


def test_optimiser_stops_when_no_allowed_area_exists():
    graph = {1: {2}, 2: {1}, 3: set()}
    with pytest.raises(ValueError):
        grow_launch_area(graph, lambda area_zone_ids: 0.0, min_zones=3, max_zones=3)


def test_optimiser_gives_the_same_answer_twice():
    random_generator = np.random.default_rng(TOY_SEED)
    graph = make_random_toy_graph(random_generator, 10)
    score_function = make_toy_score_function(random_generator, 10)
    first = grow_launch_area(graph, score_function, min_zones=2, max_zones=5)
    second = grow_launch_area(graph, score_function, min_zones=2, max_zones=5)
    assert first["zone_ids"] == second["zone_ids"]


def test_top_demand_seeds_are_ordered_by_share_then_id():
    assert pick_top_demand_seeds({1: 0.1, 2: 0.4, 3: 0.4, 4: 0.1}, 3) == [2, 3, 1]


# ----------------------------------------------------------------- proxy score

def make_proxy_toy_tables():
    """Two zones 1 mile apart. Zone 1 has 10 requests per hour, zone 2 has none."""
    zones = pd.DataFrame({
        "zone_id": [1, 2],
        "centroid_x_metres": [0.0, launch_area.METRES_PER_MILE],
        "centroid_y_metres": [0.0, 0.0],
        "area_square_metres": [launch_area.SQUARE_METRES_PER_SQUARE_MILE] * 2,
    })
    mean_hourly_requests = pd.DataFrame({"zone_id": [1, 2], "mean_hourly_requests": [10.0, 0.0]})
    zone_pairs = pd.DataFrame({
        "pickup_zone_id": [1, 1, 2, 2], "dropoff_zone_id": [1, 2, 1, 2],
        "trip_count": [60, 40, 0, 0],
        "median_passenger_miles": [2.0, 3.0, np.nan, np.nan],
        "median_trip_minutes": [12.0, 24.0, np.nan, np.nan],
    })
    return zones, mean_hourly_requests, zone_pairs


def test_proxy_score_of_an_area_without_demand_is_minus_idle_cost():
    proxy_inputs = build_proxy_inputs(*make_proxy_toy_tables())
    score = make_proxy_score(proxy_inputs, fleet_cars=50)
    assert score([2]) == pytest.approx(-launch_area.IDLE_COST_PER_CAR_HOUR_USD)


def test_proxy_score_matches_a_hand_calculation_for_one_zone():
    zones, mean_hourly_requests, zone_pairs = make_proxy_toy_tables()
    proxy_inputs = build_proxy_inputs(zones, mean_hourly_requests, zone_pairs)
    fleet_cars = 50
    speed_mph = (60 * 2.0 + 40 * 3.0) / (60 * 0.2 + 40 * 0.4)
    pickup_miles = launch_area.PICKUP_DISTANCE_FACTOR * math.sqrt(1.0 / fleet_cars)
    empty_share = launch_area.EMPTY_MILES_SHARE_OF_LOADED_MILES

    def money_and_hours(loaded_miles, loaded_hours, empty_miles):
        revenue = launch_area.BASE_FARE_USD + launch_area.REVENUE_PER_MILE_USD * loaded_miles
        cost = launch_area.COST_PER_MILE_USD * (loaded_miles + empty_miles + pickup_miles)
        return revenue - cost, loaded_hours + (empty_miles + pickup_miles) / speed_mph

    money_to_zone_1, hours_to_zone_1 = money_and_hours(2.0, 0.2, empty_share * 2.0)
    money_to_zone_2, hours_to_zone_2 = money_and_hours(3.0, 0.4, 1.0)
    net_per_request = 0.6 * money_to_zone_1 + 0.4 * money_to_zone_2
    hours_per_request = 0.6 * hours_to_zone_1 + 0.4 * hours_to_zone_2
    cars_needed = 10.0 * hours_per_request
    busy_cars = fleet_cars * (1 - math.exp(-cars_needed / fleet_cars))
    profit_per_hour = (busy_cars / cars_needed * 10.0 * net_per_request
                       - launch_area.IDLE_COST_PER_CAR_HOUR_USD * (fleet_cars - busy_cars))

    score = make_proxy_score(proxy_inputs, fleet_cars=fleet_cars)
    assert score([1]) == pytest.approx(profit_per_hour / fleet_cars)


def test_proxy_inputs_stop_on_unknown_zone_in_pair_table():
    zones, mean_hourly_requests, zone_pairs = make_proxy_toy_tables()
    zone_pairs.loc[0, "dropoff_zone_id"] = 999
    with pytest.raises(ValueError):
        build_proxy_inputs(zones, mean_hourly_requests, zone_pairs)
