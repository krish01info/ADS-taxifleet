"""Phase 5: choose the launch area, a connected set of taxi zones.

This file does four jobs, in this order:
1. Build the zone graph: which zone touches which (polygon borders plus a short,
   documented list of bridge and tunnel links that the polygons cannot see).
2. Define the fixed baseline area: all Manhattan zones plus Downtown Brooklyn.
3. Search for a good connected area with a greedy "grow from a seed zone" step
   followed by a local search (add, remove or swap one zone at a time).
4. Give a cheap, explainable proxy score (expected profit per car-hour) computed
   from TRAINING data only, so the search can run without the fleet simulator.

The search does not care how an area is scored: it is given a function
score_function(area_zone_ids) -> profit per car. The proxy score below is one such
function; the fleet simulator can be plugged in the same way.
"""

import itertools
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_MARKER_FILE = "repo_instructions.md"
RELEASE_DIRECTORY = "data/releases/phase1-2024-v1"
CORRECTED_NEIGHBOURS_DIRECTORY = (
    "data/processed/phase1/zone_neighbours-fixed-20261115/zone_neighbours"
)
TRAIN_PERIOD_LABEL = "train"
METRES_PER_MILE = 1609.344
SQUARE_METRES_PER_SQUARE_MILE = METRES_PER_MILE**2

# Proxy score settings. These are nominal planning numbers, not fitted values.
# The team has not fixed the price rates yet (see docs/decisions.md).
BASE_FARE_USD = 2.50
REVENUE_PER_MILE_USD = 1.60
COST_PER_MILE_USD = 1.36
IDLE_COST_PER_CAR_HOUR_USD = 2.00
EMPTY_MILES_SHARE_OF_LOADED_MILES = 0.25
PICKUP_DISTANCE_FACTOR = 0.5
NOMINAL_FLEET_CARS = 1000

SCORE_IMPROVEMENT_TOLERANCE = 1e-9
MAX_LOCAL_SEARCH_STEPS = 1000

# Downtown Brooklyn is defined by zone names, which must exist in the zones table.
DOWNTOWN_BROOKLYN_ZONE_NAMES = (
    "Downtown Brooklyn/MetroTech",
    "Brooklyn Heights",
    "DUMBO/Vinegar Hill",
    "Boerum Hill",
    "Fort Greene",
)
# Governor's, Ellis and Liberty Island: ferry-only, no road, no neighbour. They are
# left out of the baseline because no car can drive there.
FERRY_ONLY_ZONE_IDS = (103, 104, 105)

LINK_TYPE_RIVER_CROSSING = "river_crossing"
LINK_TYPE_AIRPORT_ROAD = "airport_road"
BRIDGE_LINK_COLUMNS = ("zone_id_a", "zone_id_b", "crossing", "link_type", "reason")

# Extra zone-to-zone links for crossings that two polygons cannot show because
# water lies between them. Each row: (zone A, zone B, crossing, type, reason).
# This list is a team decision. Pass bridge_links=None to build_zone_graph to
# switch it off, or pass a shorter list.
BRIDGE_LINKS = (
    (231, 65, "Brooklyn Bridge", LINK_TYPE_RIVER_CROSSING,
     "Civic Center end to Downtown Brooklyn end"),
    (231, 66, "Brooklyn Bridge", LINK_TYPE_RIVER_CROSSING,
     "Civic Center end to the DUMBO approach"),
    (45, 65, "Manhattan Bridge", LINK_TYPE_RIVER_CROSSING,
     "Chinatown end to Downtown Brooklyn"),
    (45, 66, "Manhattan Bridge", LINK_TYPE_RIVER_CROSSING,
     "Chinatown end to the DUMBO approach"),
    (148, 256, "Williamsburg Bridge", LINK_TYPE_RIVER_CROSSING,
     "Lower East Side end to Williamsburg South Side"),
    (148, 217, "Williamsburg Bridge", LINK_TYPE_RIVER_CROSSING,
     "Lower East Side end to South Williamsburg"),
    (12, 54, "Brooklyn-Battery Tunnel", LINK_TYPE_RIVER_CROSSING,
     "Battery Park portal to Columbia Street portal"),
    (12, 195, "Brooklyn-Battery Tunnel", LINK_TYPE_RIVER_CROSSING,
     "Battery Park portal to the Red Hook side"),
    (140, 146, "Queensboro Bridge", LINK_TYPE_RIVER_CROSSING,
     "Manhattan 59th Street end to Queens Plaza"),
    (229, 146, "Queensboro Bridge", LINK_TYPE_RIVER_CROSSING,
     "Sutton Place ramps to Queens Plaza"),
    (140, 193, "Queensboro Bridge", LINK_TYPE_RIVER_CROSSING,
     "Manhattan end to Queensbridge"),
    (233, 145, "Queens-Midtown Tunnel", LINK_TYPE_RIVER_CROSSING,
     "Turtle Bay portal to Hunters Point portal"),
    (170, 145, "Queens-Midtown Tunnel", LINK_TYPE_RIVER_CROSSING,
     "Murray Hill portal to Hunters Point portal"),
    (202, 140, "Roosevelt Island tram and Queensboro Bridge", LINK_TYPE_RIVER_CROSSING,
     "Roosevelt Island is otherwise cut off from Manhattan"),
    (202, 193, "Roosevelt Island Bridge", LINK_TYPE_RIVER_CROSSING,
     "Roosevelt Island to Astoria side (Queensbridge)"),
    (194, 8, "Triborough (RFK) Bridge", LINK_TYPE_RIVER_CROSSING,
     "Randalls Island to Astoria Park"),
    (194, 168, "Triborough (RFK) Bridge", LINK_TYPE_RIVER_CROSSING,
     "Randalls Island to Mott Haven, Bronx"),
    (74, 168, "Third Avenue and Willis Avenue Bridges", LINK_TYPE_RIVER_CROSSING,
     "East Harlem to Mott Haven across the Harlem River"),
    (153, 127, "Broadway Bridge and University Heights Bridge", LINK_TYPE_RIVER_CROSSING,
     "Marble Hill is in Manhattan but sits on the mainland; its road link to Inwood"),
    (41, 168, "Madison Avenue Bridge", LINK_TYPE_RIVER_CROSSING,
     "Central Harlem to Mott Haven"),
    (42, 247, "Macombs Dam Bridge", LINK_TYPE_RIVER_CROSSING,
     "Central Harlem North to the Yankee Stadium area"),
    (244, 235, "Washington Bridge", LINK_TYPE_RIVER_CROSSING,
     "Washington Heights to University Heights"),
    (244, 119, "Alexander Hamilton Bridge", LINK_TYPE_RIVER_CROSSING,
     "Washington Heights to Highbridge, Bronx"),
    (15, 58, "Throgs Neck Bridge", LINK_TYPE_RIVER_CROSSING,
     "Bay Terrace (Queens) to Country Club (Bronx)"),
    (252, 58, "Bronx-Whitestone Bridge", LINK_TYPE_RIVER_CROSSING,
     "Whitestone (Queens) to Country Club (Bronx)"),
    (46, 184, "City Island Bridge", LINK_TYPE_RIVER_CROSSING,
     "City Island has no polygon neighbour; road runs to Pelham Bay Park"),
    (6, 14, "Verrazzano-Narrows Bridge", LINK_TYPE_RIVER_CROSSING,
     "Fort Wadsworth (Staten Island) to Bay Ridge"),
    (6, 228, "Verrazzano-Narrows Bridge", LINK_TYPE_RIVER_CROSSING,
     "Fort Wadsworth (Staten Island) to Sunset Park West"),
    (30, 117, "Cross Bay Veterans Bridge", LINK_TYPE_RIVER_CROSSING,
     "Broad Channel to the Rockaway peninsula"),
    (27, 154, "Marine Parkway Bridge", LINK_TYPE_RIVER_CROSSING,
     "Rockaway (Riis Beach) to Floyd Bennett Field, Brooklyn"),
    (132, 145, "Van Wyck, Long Island Expressway and Midtown Tunnel", LINK_TYPE_AIRPORT_ROAD,
     "JFK Airport to Manhattan-bound traffic at Hunters Point"),
    (138, 194, "Grand Central Parkway to Triborough Bridge", LINK_TYPE_AIRPORT_ROAD,
     "LaGuardia Airport to Randalls Island and Manhattan"),
    (1, 125, "Holland Tunnel", LINK_TYPE_AIRPORT_ROAD,
     "Newark Airport to Lower Manhattan (Hudson Square)"),
    (1, 50, "Lincoln Tunnel", LINK_TYPE_AIRPORT_ROAD,
     "Newark Airport to Midtown West (Clinton West)"),
    (1, 156, "Goethals Bridge", LINK_TYPE_AIRPORT_ROAD,
     "Newark Airport to Staten Island (Mariners Harbor)"),
)


# ---------------------------------------------------------------- loading data

def find_repo_root(start_path=None):
    """Walk up from start_path (default: this file) to the folder holding repo_instructions.md."""
    current = Path(start_path if start_path is not None else __file__).resolve()
    for folder in [current, *current.parents]:
        if (folder / REPO_MARKER_FILE).exists():
            return folder
    raise FileNotFoundError(f"No parent folder contains {REPO_MARKER_FILE}")


def read_parquet_directory(directory, columns="*", where_clause="true"):
    """Read a folder of parquet files with duckdb (pyarrow is blocked on this machine)."""
    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"Missing data folder: {directory}")
    pattern = (directory / "**" / "*.parquet").as_posix()
    query = (f"select {columns} from read_parquet('{pattern}', hive_partitioning=true) "
             f"where {where_clause}")
    return duckdb.connect().execute(query).df()


def release_table_directory(repo_root, table_name):
    """Return the folder of one table of the Phase 1 release, using its table_index.json."""
    index_path = Path(repo_root) / RELEASE_DIRECTORY / "metadata" / "table_index.json"
    table_index = json.loads(index_path.read_text())
    if table_name not in table_index:
        raise KeyError(f"{table_name} is not in {index_path}")
    return Path(repo_root) / table_index[table_name]["path"]


def load_zones(repo_root):
    """Load the 263 zones (id, name, borough, centroid in metres, area in square metres)."""
    columns = ("zone_id, zone_name, borough, is_airport, area_square_metres, "
               "centroid_x_metres, centroid_y_metres")
    zones = read_parquet_directory(release_table_directory(repo_root, "zones"), columns)
    return zones.sort_values("zone_id").reset_index(drop=True)


def load_polygon_neighbours(repo_root):
    """Load the CORRECTED neighbour table (651 pairs as 1,302 directed rows)."""
    return read_parquet_directory(Path(repo_root) / CORRECTED_NEIGHBOURS_DIRECTORY)


def load_training_mean_hourly_requests(repo_root):
    """Mean requests per hour for each zone, from TRAINING zone-hours only (Jan-Aug 2024).

    Zone-hours whose request count is unknown (incomplete coverage) are left out,
    not counted as zero.
    """
    where_clause = f"period_label = '{TRAIN_PERIOD_LABEL}' and request_count is not null"
    columns = ("zone_id, avg(request_count) as mean_hourly_requests, "
               "count(*) as hours_counted")
    directory = release_table_directory(repo_root, "zone_hour")
    return read_parquet_directory(
        directory, columns, where_clause + " group by zone_id order by zone_id")


def load_training_zone_pairs(repo_root):
    """Load the train-only origin-destination table (counts and median miles/minutes)."""
    columns = ("pickup_zone_id, dropoff_zone_id, trip_count, median_passenger_miles, "
               "median_trip_minutes")
    where_clause = f"period_label = '{TRAIN_PERIOD_LABEL}' and is_training_reference"
    directory = release_table_directory(repo_root, "training_zone_pair_reference")
    return read_parquet_directory(directory, columns, where_clause)


# ------------------------------------------------------------------ zone graph

def bridge_links_table():
    """Return the BRIDGE_LINKS constant as a table (this is what is saved as a CSV)."""
    return pd.DataFrame(list(BRIDGE_LINKS), columns=list(BRIDGE_LINK_COLUMNS))


def select_bridge_links(link_types):
    """Return only the bridge links whose type is in link_types (the on/off switch by type)."""
    return [link for link in BRIDGE_LINKS if link[3] in link_types]


def build_zone_graph(zones, polygon_neighbours, bridge_links=None):
    """Build the zone graph as a dict: zone_id -> set of neighbouring zone_ids.

    Takes the zones table, the polygon neighbour table (columns zone_id and
    neighbour_zone_id) and an optional list of bridge links (only the first two
    items of each link, the two zone ids, are used). The graph is symmetric: if
    A is a neighbour of B, B is a neighbour of A. Unknown zone ids stop the run.
    """
    known_zone_ids = set(zones["zone_id"])
    graph = {zone_id: set() for zone_id in known_zone_ids}
    pairs = list(zip(polygon_neighbours["zone_id"], polygon_neighbours["neighbour_zone_id"]))
    for link in bridge_links or []:
        pairs.append((link[0], link[1]))
    for zone_id, other_zone_id in pairs:
        if zone_id not in known_zone_ids or other_zone_id not in known_zone_ids:
            raise ValueError(f"Link uses an unknown zone id: {zone_id}, {other_zone_id}")
        if zone_id == other_zone_id:
            raise ValueError(f"Zone {zone_id} is linked to itself")
        graph[zone_id].add(other_zone_id)
        graph[other_zone_id].add(zone_id)
    return graph


def count_new_edges(graph_without_links, graph_with_links):
    """Count the undirected zone pairs that exist only in the graph that has bridge links."""
    edges_without = sum(len(neighbours) for neighbours in graph_without_links.values()) // 2
    edges_with = sum(len(neighbours) for neighbours in graph_with_links.values()) // 2
    return edges_with - edges_without


def find_connected_groups(graph):
    """Split the graph into connected groups; returns a list of sets, biggest first."""
    unvisited = set(graph)
    groups = []
    while unvisited:
        start_zone_id = min(unvisited)
        group = {start_zone_id}
        to_visit = [start_zone_id]
        while to_visit:
            zone_id = to_visit.pop()
            for neighbour_id in graph[zone_id]:
                if neighbour_id not in group:
                    group.add(neighbour_id)
                    to_visit.append(neighbour_id)
        groups.append(group)
        unvisited -= group
    groups.sort(key=lambda group: (-len(group), min(group)))
    return groups


def area_is_connected(graph, area_zone_ids):
    """True when every zone of the area can be reached from every other, staying inside the area."""
    area = set(area_zone_ids)
    if not area:
        return False
    start_zone_id = min(area)
    reached = {start_zone_id}
    to_visit = [start_zone_id]
    while to_visit:
        zone_id = to_visit.pop()
        for neighbour_id in graph[zone_id] & area:
            if neighbour_id not in reached:
                reached.add(neighbour_id)
                to_visit.append(neighbour_id)
    return reached == area


# -------------------------------------------------------------- baseline area

def fixed_baseline_area(zones):
    """The fixed baseline: every Manhattan zone plus Downtown Brooklyn, as a sorted id list.

    Ferry-only island zones (FERRY_ONLY_ZONE_IDS) are left out. Downtown Brooklyn
    is the zones named in DOWNTOWN_BROOKLYN_ZONE_NAMES; a missing name stops the run.
    """
    manhattan_ids = set(zones.loc[zones["borough"] == "Manhattan", "zone_id"])
    brooklyn = zones[zones["borough"] == "Brooklyn"]
    downtown_brooklyn = brooklyn[brooklyn["zone_name"].isin(DOWNTOWN_BROOKLYN_ZONE_NAMES)]
    if len(downtown_brooklyn) != len(DOWNTOWN_BROOKLYN_ZONE_NAMES):
        raise ValueError("A Downtown Brooklyn zone name was not found exactly once in zones")
    baseline_ids = (manhattan_ids - set(FERRY_ONLY_ZONE_IDS)) | set(downtown_brooklyn["zone_id"])
    return sorted(baseline_ids)


# ------------------------------------------------------------------- optimiser

def make_cached_scorer(score_function):
    """Wrap score_function so each distinct area is scored once; returns (scorer, cache)."""
    cache = {}

    def scorer(area_zone_ids):
        key = frozenset(area_zone_ids)
        if key not in cache:
            cache[key] = float(score_function(sorted(key)))
        return cache[key]

    return scorer, cache


def demand_share_of_area(area_zone_ids, zone_demand_share):
    """Share of all demand that falls in the area (1.0 when no demand table is given)."""
    if zone_demand_share is None:
        return 1.0
    return sum(zone_demand_share.get(zone_id, 0.0) for zone_id in area_zone_ids)


def area_is_allowed(graph, area, min_zones, max_zones, zone_demand_share, min_demand_share):
    """True when the area is connected, within the size limits and covers enough demand."""
    if not min_zones <= len(area) <= max_zones:
        return False
    if demand_share_of_area(area, zone_demand_share) < min_demand_share:
        return False
    return area_is_connected(graph, area)


def find_boundary_zones(graph, area):
    """Zones outside the area that touch it, in id order."""
    boundary = set()
    for zone_id in area:
        boundary |= graph[zone_id]
    return sorted(boundary - area)


def grow_from_seed(graph, scorer, seed_zone_id, min_zones, max_zones, zone_demand_share,
                   min_demand_share):
    """Greedy growth: start at one zone and keep adding the boundary zone that scores best.

    Returns the best allowed area seen on the way (or None if none was allowed).
    """
    area = {seed_zone_id}
    best_area, best_score = None, -np.inf
    while True:
        allowed = area_is_allowed(graph, area, min_zones, max_zones, zone_demand_share,
                                  min_demand_share)
        if allowed and scorer(area) > best_score:
            best_area, best_score = set(area), scorer(area)
        boundary = find_boundary_zones(graph, area)
        if len(area) >= max_zones or not boundary:
            return best_area
        area.add(max(boundary, key=lambda zone_id: (scorer(area | {zone_id}), -zone_id)))


def list_neighbour_areas(graph, area, min_zones, max_zones):
    """All areas one move away: add one zone, remove one zone, or swap one zone for another."""
    boundary = find_boundary_zones(graph, area)
    neighbour_areas = []
    if len(area) < max_zones:
        neighbour_areas += [area | {zone_id} for zone_id in boundary]
    if len(area) > min_zones:
        neighbour_areas += [area - {zone_id} for zone_id in sorted(area)]
    for removed_zone_id in sorted(area):
        for added_zone_id in boundary:
            neighbour_areas.append((area - {removed_zone_id}) | {added_zone_id})
    return neighbour_areas


def improve_by_local_search(graph, scorer, start_area, min_zones, max_zones, zone_demand_share,
                            min_demand_share):
    """Keep making the best add, remove or swap move while it raises the score."""
    area = set(start_area)
    for _ in range(MAX_LOCAL_SEARCH_STEPS):
        best_area, best_score = None, scorer(area) + SCORE_IMPROVEMENT_TOLERANCE
        for candidate in list_neighbour_areas(graph, area, min_zones, max_zones):
            if not area_is_allowed(graph, candidate, min_zones, max_zones, zone_demand_share,
                                   min_demand_share):
                continue
            candidate_score = scorer(candidate)
            if candidate_score > best_score:
                best_area, best_score = candidate, candidate_score
        if best_area is None:
            return area
        area = best_area
    raise RuntimeError("Local search did not settle within MAX_LOCAL_SEARCH_STEPS")


def grow_launch_area(graph, score_function, min_zones, max_zones, seed_zone_ids=None,
                     zone_demand_share=None, min_demand_share=0.0):
    """Find a good connected launch area: grow from each seed zone, then improve by local search.

    graph: dict zone_id -> set of neighbour ids.
    score_function: score_function(list_of_zone_ids) -> profit per car (higher is better).
    min_zones, max_zones: allowed size of the area (set them equal for a fixed size).
    seed_zone_ids: zones to start from (default: every zone in the graph).
    zone_demand_share, min_demand_share: dict zone_id -> share of all demand, and the
        least share the area must cover. Without the dict the share rule is off.
    Nothing is random, so the same inputs give the same area. Ties go to the lowest zone id.
    Returns a dict: zone_ids (sorted list), score, demand_share, seed_zone_id, score_calls.
    """
    if min_zones < 1 or max_zones < min_zones:
        raise ValueError("Need 1 <= min_zones <= max_zones")
    if seed_zone_ids is None:
        seed_zone_ids = sorted(graph)
    scorer, cache = make_cached_scorer(score_function)
    best_result = None
    for seed_zone_id in seed_zone_ids:
        grown_area = grow_from_seed(graph, scorer, seed_zone_id, min_zones, max_zones,
                                    zone_demand_share, min_demand_share)
        if grown_area is None:
            continue
        final_area = improve_by_local_search(graph, scorer, grown_area, min_zones, max_zones,
                                             zone_demand_share, min_demand_share)
        if best_result is None or scorer(final_area) > best_result["score"]:
            best_result = {"zone_ids": sorted(final_area), "score": scorer(final_area),
                           "seed_zone_id": seed_zone_id}
    if best_result is None:
        raise ValueError("No seed zone could grow into an allowed area (size or demand-share "
                         "limits cannot be met inside its connected group)")
    best_result["demand_share"] = demand_share_of_area(best_result["zone_ids"], zone_demand_share)
    best_result["score_calls"] = len(cache)
    return best_result


def find_best_area_by_brute_force(graph, score_function, min_zones, max_zones,
                                  zone_demand_share=None, min_demand_share=0.0):
    """Try every allowed area of a SMALL graph and return the best one (used to check the search)."""
    best_area, best_score = None, -np.inf
    for size in range(min_zones, max_zones + 1):
        for combination in itertools.combinations(sorted(graph), size):
            area = set(combination)
            if not area_is_allowed(graph, area, min_zones, max_zones, zone_demand_share,
                                   min_demand_share):
                continue
            score = float(score_function(sorted(area)))
            if score > best_score:
                best_area, best_score = sorted(area), score
    if best_area is None:
        raise ValueError("No allowed area exists")
    return {"zone_ids": best_area, "score": best_score}


def pick_top_demand_seeds(zone_demand_share, count):
    """The `count` zones with the largest demand share (ties by lowest id): good seed zones."""
    ranked = sorted(zone_demand_share, key=lambda zone_id: (-zone_demand_share[zone_id], zone_id))
    return ranked[:count]


# ----------------------------------------------------------------- proxy score

def build_proxy_inputs(zones, mean_hourly_requests, zone_pairs):
    """Prepare the matrices the proxy score needs (training data only).

    zones: zones table. mean_hourly_requests: table zone_id, mean_hourly_requests.
    zone_pairs: train-only origin-destination table. Returns a dict of numpy arrays
    indexed by zone position, plus the zone id list.
    """
    zone_ids = list(zones["zone_id"])
    position_of = {zone_id: position for position, zone_id in enumerate(zone_ids)}
    zone_count = len(zone_ids)
    unknown = ~(zone_pairs["pickup_zone_id"].isin(position_of)
                & zone_pairs["dropoff_zone_id"].isin(position_of))
    if unknown.any():
        raise ValueError(f"{int(unknown.sum())} zone pairs use zone ids missing from zones")

    pairs = zone_pairs[zone_pairs["trip_count"] > 0]
    rows = pairs["pickup_zone_id"].map(position_of).to_numpy()
    columns = pairs["dropoff_zone_id"].map(position_of).to_numpy()
    trip_counts = np.zeros((zone_count, zone_count))
    loaded_miles = np.zeros((zone_count, zone_count))
    loaded_hours = np.zeros((zone_count, zone_count))
    trip_counts[rows, columns] = pairs["trip_count"].to_numpy()
    loaded_miles[rows, columns] = pairs["median_passenger_miles"].to_numpy()
    loaded_hours[rows, columns] = pairs["median_trip_minutes"].to_numpy() / 60.0
    if np.isnan(loaded_miles).any() or np.isnan(loaded_hours).any():
        raise ValueError("A zone pair with trips has no median miles or minutes")

    trips_from_zone = trip_counts.sum(axis=1, keepdims=True)
    destination_share = np.divide(trip_counts, trips_from_zone, out=np.zeros_like(trip_counts),
                                  where=trips_from_zone > 0)
    average_loaded_speed_mph = (trip_counts * loaded_miles).sum() / (trip_counts * loaded_hours).sum()

    requests = np.zeros(zone_count)
    for row in mean_hourly_requests.itertuples():
        requests[position_of[row.zone_id]] = row.mean_hourly_requests
    centroids_miles = zones[["centroid_x_metres", "centroid_y_metres"]].to_numpy() / METRES_PER_MILE
    differences = centroids_miles[:, None, :] - centroids_miles[None, :, :]
    return {
        "zone_ids": zone_ids,
        "position_of": position_of,
        "mean_hourly_requests": requests,
        "destination_share": destination_share,
        "loaded_miles": loaded_miles,
        "loaded_hours": loaded_hours,
        "average_loaded_speed_mph": average_loaded_speed_mph,
        "centroid_distance_miles": np.sqrt((differences**2).sum(axis=2)),
        "area_square_miles": zones["area_square_metres"].to_numpy() / SQUARE_METRES_PER_SQUARE_MILE,
    }


def make_proxy_score(proxy_inputs, fleet_cars=NOMINAL_FLEET_CARS):
    """Return score(area_zone_ids) -> expected profit per car-hour (USD) of a fleet of fleet_cars.

    How the score works, for an area A (all numbers from TRAINING data):
    1. Requests: each zone z in A gets its training mean requests per hour. Each
       request goes to a destination d with the training share of trips z -> d and
       the median miles and minutes of that pair.
    2. Money per request: revenue = BASE_FARE_USD + REVENUE_PER_MILE_USD x loaded miles.
       Cost = COST_PER_MILE_USD x ALL miles. All miles = loaded miles + empty miles
       + pickup miles. Empty miles: if d is inside A, a fixed share of the loaded
       miles (EMPTY_MILES_SHARE_OF_LOADED_MILES); if d is outside A, the car drives
       back empty to the nearest zone of A (straight line between centroids).
       Pickup miles: PICKUP_DISTANCE_FACTOR x sqrt(area in sq miles / cars), the
       usual nearest-neighbour distance, so a bigger area means longer pickups.
    3. Diminishing returns: the cars needed to serve everything is
       needed = requests per hour x hours per request (loaded + empty + pickup).
       Busy cars = fleet x (1 - exp(-needed / fleet)). It rises with demand but
       flattens near the fleet size, so extra demand helps less and less. Requests
       served = demand x busy cars / needed.
    4. Profit per hour = served requests x money per request - idle cost x idle cars.
       The score is this divided by the fleet size.
    """
    position_of = proxy_inputs["position_of"]
    speed_mph = proxy_inputs["average_loaded_speed_mph"]

    def score(area_zone_ids):
        positions = np.array([position_of[zone_id] for zone_id in area_zone_ids])
        if len(positions) == 0:
            raise ValueError("Cannot score an empty area")
        is_inside = np.zeros(len(proxy_inputs["zone_ids"]), dtype=bool)
        is_inside[positions] = True
        requests = proxy_inputs["mean_hourly_requests"][positions]
        destination_share = proxy_inputs["destination_share"][positions]
        loaded_miles = proxy_inputs["loaded_miles"][positions]
        loaded_hours = proxy_inputs["loaded_hours"][positions]

        distance_to_area = proxy_inputs["centroid_distance_miles"][:, positions].min(axis=1)
        empty_miles = np.where(is_inside[None, :],
                               EMPTY_MILES_SHARE_OF_LOADED_MILES * loaded_miles,
                               distance_to_area[None, :])
        area_square_miles = proxy_inputs["area_square_miles"][positions].sum()
        pickup_miles = PICKUP_DISTANCE_FACTOR * np.sqrt(area_square_miles / fleet_cars)

        revenue_usd = BASE_FARE_USD + REVENUE_PER_MILE_USD * loaded_miles
        cost_usd = COST_PER_MILE_USD * (loaded_miles + empty_miles + pickup_miles)
        hours = loaded_hours + (empty_miles + pickup_miles) / speed_mph

        net_usd_per_request = (destination_share * (revenue_usd - cost_usd)).sum(axis=1)
        hours_per_request = (destination_share * hours).sum(axis=1)
        cars_needed = (requests * hours_per_request).sum()
        busy_cars = fleet_cars * (1.0 - np.exp(-cars_needed / fleet_cars))
        share_served = busy_cars / cars_needed if cars_needed > 0 else 0.0
        profit_usd_per_hour = (share_served * (requests * net_usd_per_request).sum()
                               - IDLE_COST_PER_CAR_HOUR_USD * (fleet_cars - busy_cars))
        return profit_usd_per_hour / fleet_cars

    return score


def make_zone_demand_share(mean_hourly_requests):
    """Each zone's share of the total training demand, as a dict zone_id -> share."""
    total_requests = mean_hourly_requests["mean_hourly_requests"].sum()
    return {int(row.zone_id): row.mean_hourly_requests / total_requests
            for row in mean_hourly_requests.itertuples()}
