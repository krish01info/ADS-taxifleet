"""Phase 4: the fleet simulator core. It replays one civil day at a time on a 15-minute grid.

Where it fits: Phase 1 gives the demand and trip tables, Phase 3 gives a forecast, Phase 5
chooses the launch area. This file takes those three things and says how much profit a fleet
of N cars earns under a placement policy. The Phase 6 runner imports the functions below.

How to call it (the interface the other phases use)
    inputs = load_simulation_inputs()                     # reads the release once
    price = fit_common_price(inputs["training_zone_hour"])  # common price rule, see below
    settings = make_settings(price["base_charge_usd"], price["per_mile_rate_usd"],
                             cost_per_mile_usd=1.36, idle_cost_per_car_hour_usd=2.0)
    forecast_by_date = {day: make_previous_week_forecast(inputs, day) for day in days}
    table = simulate_days(inputs, days, forecast_by_date, policies=POLICIES, n_cars=200,
                          allowed_zone_ids=zone_ids_of_launch_area, settings=settings, seed=1)
    table has one row per (date, policy). simulate_day(...) does one day and also returns a
    per-zone table (requests, served, served share) for the service-gap check.

Shapes: a forecast or actual demand is an array (24, number_of_zones), rows = New York civil
hours 0-23, columns = zones in the order of inputs["zone_ids"]. Values are REAL request counts
per zone-hour (not scaled down). The simulator scales them by settings["demand_scale"].

Policies (decided from the forecast only; the simulator never shows a policy the actuals)
    stay        cars wait where their last trip ended (H5 baseline).
    ride_count  at each hour start, idle cars are re-spread in proportion to forecast requests.
    profit      at each hour start, idle cars are placed one at a time where the next car adds
                the most expected profit, minus the cost of the empty drive (H3).
    equal       idle cars are spread evenly over the allowed zones (extra comparison).
    random      idle cars are put in seeded random allowed zones (extra comparison).
    wait_or_move a car that has just finished a trip drives empty to its best neighbour if its
                own zone's forecast is below move_ratio x that neighbour's forecast (H5).

Rules and simplifications (record these in docs/decisions.md)
    1. Hourly demand is split evenly into four 15-minute slots (a seeded multinomial draw),
       because the release only has hourly counts. Requests are assumed to arrive at the
       middle of their slot.
    2. Realised demand = the ACTUAL observed request_count of each zone-hour, thinned to
       demand_scale (default 1%) by a seeded binomial draw, so the fleet is hundreds of cars.
       Zone-hours with null coverage (DST hours) are treated as missing (zero) and reported.
    3. Each request draws its destination from the training OD table of its origin zone
       (probability proportional to trip_count). Trip miles and minutes are the OD medians.
       Pairs without training trips use straight-line distance x detour factor, and minutes
       from an average speed; both are estimated from supported training pairs.
    4. Service area: a request counts only if it starts in an allowed zone. If its drawn
       destination is outside the allowed zones it is dropped (a geofence) and counted in
       requests_dropped_outside. So every car always stays inside the allowed zones.
    5. Matching per slot: an idle car in the same zone first, else the nearest idle car in a
       zone within max_pickup_hops neighbour steps, else the request is lost. A pickup from
       the same zone has no empty miles; a pickup from another zone costs the OD median
       miles/minutes between the two zones and the car is busy for that time.
    6. Revenue per served trip = base_charge_usd + per_mile_rate_usd x passenger miles, the
       same in every zone, no time charge, no tips. Cost = the origin zone's training mean
       toll + cost_per_mile_usd x (passenger miles + empty miles) + idle cost per idle
       car-hour. Trips that cross midnight are booked fully on the day they start, but their
       busy time is cut at midnight.
    7. Each day starts fresh: cars are put proportional to the forecast of hour 0 (the same
       start for every policy, then the policy may re-place them for free at hour 0).
       From hour 1 on, moves are paid for. Placement only moves cars that are idle at the
       hour start and ignores cars still driving.
    8. The minimum service level is reported (per-zone served share, lowest zone share), not
       enforced inside the simulator.
"""

import json
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 4
HOURS_PER_DAY = 24
SLOTS_PER_DAY = SLOTS_PER_HOUR * HOURS_PER_DAY
MINUTES_PER_HOUR = 60
DAY_MINUTES = HOURS_PER_DAY * MINUTES_PER_HOUR
REQUEST_MINUTE_IN_SLOT = SLOT_MINUTES / 2
DAYS_PER_WEEK = 7

DEFAULT_SEED = 1
DEMAND_SCALE = 0.01
COST_PER_MILE_USD = 1.36
IDLE_COST_PER_CAR_HOUR_USD = 2.0
MAX_PICKUP_HOPS = 1
MOVE_RATIO = 0.5
REQUESTS_PER_CAR_HOUR = 2.0

MIN_STRAIGHT_MILES_FOR_DETOUR = 0.5
MIN_FALLBACK_TRIP_MILES = 0.1
DEMAND_STREAM = 0
POLICY_STREAM = 1

POLICIES = ("stay", "ride_count", "profit", "equal", "random", "wait_or_move")
HOURLY_PLACEMENT_POLICIES = ("ride_count", "profit", "equal", "random")

RELEASE_DIR = "data/releases/phase1-2024-v1"
NEIGHBOURS_DIR = "data/processed/phase1/zone_neighbours-fixed-20261115/zone_neighbours"
TRAIN_PERIOD_LABEL = "train"
UNSUPPORTED_LABEL = "unsupported"
SUPPORTED_LABEL = "supported"

DEFAULT_SETTING_VALUES = {
    "demand_scale": DEMAND_SCALE,
    "cost_per_mile_usd": COST_PER_MILE_USD,
    "idle_cost_per_car_hour_usd": IDLE_COST_PER_CAR_HOUR_USD,
    "max_pickup_hops": MAX_PICKUP_HOPS,
    "move_ratio": MOVE_RATIO,
    "requests_per_car_hour": REQUESTS_PER_CAR_HOUR,
}


def find_repo_root():
    """Walk up from this file to the folder that holds repo_instructions.md."""
    folder = Path(__file__).resolve().parent
    while not (folder / "repo_instructions.md").exists():
        if folder.parent == folder:
            raise FileNotFoundError("Could not find the repo root (repo_instructions.md).")
        folder = folder.parent
    return folder


def read_parquet_folder(connection, folder):
    """Read every parquet file under a folder into a DataFrame (duckdb, no pyarrow)."""
    pattern = (Path(folder) / "**" / "*.parquet").as_posix()
    sql = f"select * from read_parquet('{pattern}', hive_partitioning=true)"
    return connection.execute(sql).df()


def make_settings(base_charge_usd, per_mile_rate_usd, **overrides):
    """Return the full settings dict: the common price plus cost and rule parameters."""
    unknown_names = set(overrides) - set(DEFAULT_SETTING_VALUES)
    if unknown_names:
        raise ValueError(f"Unknown settings: {sorted(unknown_names)}")
    settings = dict(DEFAULT_SETTING_VALUES)
    settings.update(overrides)
    settings["base_charge_usd"] = base_charge_usd
    settings["per_mile_rate_usd"] = per_mile_rate_usd
    return settings


def fit_common_price(zone_hour_training):
    """Fit base charge and per-mile rate so they match the recorded average fares.

    Least squares of valid_base_fare_sum_usd on valid_fare_count (the base charge) and
    passenger_miles_sum (the per-mile rate), over training zone-hours, no intercept.
    This only calibrates the common price rule to what riders paid on average. It is not a
    pricing recommendation, and the real fares also contained a time charge.
    """
    needed_columns = ["valid_base_fare_sum_usd", "valid_fare_count", "passenger_miles_sum"]
    missing_columns = [name for name in needed_columns if name not in zone_hour_training]
    if missing_columns:
        raise ValueError(f"Training table lacks columns: {missing_columns}")
    rows = zone_hour_training[zone_hour_training["valid_fare_count"] > 0]
    if len(rows) == 0:
        raise ValueError("No training zone-hours with valid fares to fit the price.")
    design = rows[["valid_fare_count", "passenger_miles_sum"]].to_numpy(dtype=float)
    fare_sums = rows["valid_base_fare_sum_usd"].to_numpy(dtype=float)
    (base_charge_usd, per_mile_rate_usd), _, _, _ = np.linalg.lstsq(design, fare_sums, rcond=None)
    residuals = fare_sums - design @ np.array([base_charge_usd, per_mile_rate_usd])
    total_spread = ((fare_sums - fare_sums.mean()) ** 2).sum()
    return {
        "base_charge_usd": float(base_charge_usd),
        "per_mile_rate_usd": float(per_mile_rate_usd),
        "r_squared": float(1 - (residuals**2).sum() / total_spread),
        "zone_hours_used": len(rows),
        "mean_recorded_fare_usd": float(fare_sums.sum() / rows["valid_fare_count"].sum()),
    }


def build_travel_tables(od_reference, zone_ids):
    """Build miles, minutes and destination-probability matrices from the training OD table.

    Returns a dict with miles_matrix, minutes_matrix, destination_cdf (all n x n, rows are
    the origin zone) plus the detour factor and average speed used for unsupported pairs.
    """
    zone_position = {zone_id: position for position, zone_id in enumerate(zone_ids)}
    pairs = od_reference.copy()
    pairs["origin"] = pairs["pickup_zone_id"].map(zone_position)
    pairs["destination"] = pairs["dropoff_zone_id"].map(zone_position)
    if pairs[["origin", "destination"]].isna().any().any():
        raise ValueError("OD table has zone ids that are not in the zones table.")
    is_supported = pairs["support_status"] == SUPPORTED_LABEL
    supported = pairs[is_supported]
    long_enough = supported["straight_line_centroid_miles"] >= MIN_STRAIGHT_MILES_FOR_DETOUR
    ratios = supported["median_passenger_miles"] / supported["straight_line_centroid_miles"]
    detour_factor = float(ratios[long_enough].median())
    average_speed_mph = float(
        (supported["median_passenger_miles"] * supported["trip_count"]).sum()
        / (supported["median_trip_minutes"] * supported["trip_count"]).sum()
        * MINUTES_PER_HOUR
    )
    has_median = pairs["support_status"] != UNSUPPORTED_LABEL
    fallback_miles = np.maximum(
        pairs["straight_line_centroid_miles"] * detour_factor, MIN_FALLBACK_TRIP_MILES
    )
    trip_miles = np.where(has_median, pairs["median_passenger_miles"], fallback_miles)
    trip_minutes = np.where(
        has_median, pairs["median_trip_minutes"], fallback_miles / average_speed_mph * MINUTES_PER_HOUR
    )
    zone_count = len(zone_ids)
    origin = pairs["origin"].astype(int).to_numpy()
    destination = pairs["destination"].astype(int).to_numpy()
    miles_matrix = np.zeros((zone_count, zone_count))
    minutes_matrix = np.zeros((zone_count, zone_count))
    trip_counts = np.zeros((zone_count, zone_count))
    miles_matrix[origin, destination] = trip_miles
    minutes_matrix[origin, destination] = trip_minutes
    trip_counts[origin, destination] = pairs["trip_count"].to_numpy()
    for position in np.flatnonzero(trip_counts.sum(axis=1) == 0):
        trip_counts[position, position] = 1
    destination_cdf = np.cumsum(trip_counts / trip_counts.sum(axis=1, keepdims=True), axis=1)
    destination_cdf[:, -1] = 1.0
    return {
        "miles_matrix": miles_matrix,
        "minutes_matrix": minutes_matrix,
        "destination_cdf": destination_cdf,
        "detour_factor": detour_factor,
        "average_speed_mph": average_speed_mph,
    }


def build_adjacent_matrix(neighbour_edges, zone_ids):
    """Return an n x n True/False matrix: True where two zones share a boundary."""
    zone_position = {zone_id: position for position, zone_id in enumerate(zone_ids)}
    shared = neighbour_edges[neighbour_edges["shares_boundary"]]
    adjacent = np.zeros((len(zone_ids), len(zone_ids)), dtype=bool)
    rows = shared["zone_id"].map(zone_position).to_numpy()
    columns = shared["neighbour_zone_id"].map(zone_position).to_numpy()
    adjacent[rows, columns] = True
    return adjacent


def build_within_hops_matrix(adjacent, max_pickup_hops):
    """Return True where zone b can be reached from zone a in 1..max_pickup_hops steps."""
    within_hops = adjacent.copy()
    reached = adjacent.copy()
    for _ in range(max_pickup_hops - 1):
        reached = (reached.astype(int) @ adjacent.astype(int)) > 0
        within_hops = within_hops | reached
    np.fill_diagonal(within_hops, False)
    return within_hops


def build_hourly_requests(zone_hour, zone_ids):
    """Put request_count into one (24, zones) array per New York civil day (NaN = missing)."""
    counted = zone_hour[zone_hour["request_count"].notna()]
    key_columns = ["event_date_nyc", "hour_nyc", "zone_id"]
    if counted.duplicated(subset=key_columns).any():
        raise ValueError("Two zone-hours share the same civil date, hour and zone.")
    all_days = sorted(zone_hour["event_date_nyc"].unique())
    day_position = {day: position for position, day in enumerate(all_days)}
    zone_position = {zone_id: position for position, zone_id in enumerate(zone_ids)}
    cube = np.full((len(all_days), HOURS_PER_DAY, len(zone_ids)), np.nan)
    day_index = counted["event_date_nyc"].map(day_position).to_numpy()
    zone_index = counted["zone_id"].map(zone_position).to_numpy()
    cube[day_index, counted["hour_nyc"].to_numpy(), zone_index] = counted["request_count"].to_numpy(
        dtype=float
    )
    return {day: cube[position] for day, position in day_position.items()}


def load_simulation_inputs(repo_root=None):
    """Load everything the simulator needs from the Phase 1 release, using duckdb.

    Returns a dict: zone_ids, zones, miles_matrix, minutes_matrix, destination_cdf,
    adjacent, mean_toll_usd, hourly_requests (date -> (24, zones) array), day_table
    (date, period_label, is_federal_holiday), training_zone_hour, detour_factor,
    average_speed_mph.
    """
    root = Path(repo_root) if repo_root else find_repo_root()
    release = root / RELEASE_DIR
    table_index = json.loads((release / "metadata" / "table_index.json").read_text())
    connection = duckdb.connect()
    zones = read_parquet_folder(connection, root / table_index["zones"]["path"])
    zones = zones.drop(columns=["geometry_wkb"]).sort_values("zone_id").reset_index(drop=True)
    zone_ids = zones["zone_id"].to_numpy()
    od_reference = read_parquet_folder(connection, root / table_index["training_zone_pair_reference"]["path"])
    neighbour_edges = read_parquet_folder(connection, root / NEIGHBOURS_DIR)
    zone_hour_path = (root / table_index["zone_hour"]["path"] / "**" / "*.parquet").as_posix()
    zone_hour = connection.execute(
        "select zone_id, event_date_nyc, hour_nyc, period_label, is_federal_holiday, request_count, "
        "valid_base_fare_sum_usd, valid_fare_count, valid_tolls_sum_usd, valid_tolls_count, "
        f"passenger_miles_sum from read_parquet('{zone_hour_path}', hive_partitioning=true)"
    ).df()
    zone_hour["event_date_nyc"] = pd.to_datetime(zone_hour["event_date_nyc"]).dt.date
    training = zone_hour[zone_hour["period_label"] == TRAIN_PERIOD_LABEL]
    toll_sums = training.groupby("zone_id")["valid_tolls_sum_usd"].sum().reindex(zone_ids)
    toll_counts = training.groupby("zone_id")["valid_tolls_count"].sum().reindex(zone_ids)
    mean_toll_usd = (toll_sums / toll_counts.where(toll_counts > 0)).fillna(0.0).to_numpy()
    day_table = (
        zone_hour[["event_date_nyc", "period_label", "is_federal_holiday"]]
        .drop_duplicates()
        .rename(columns={"event_date_nyc": "date"})
        .sort_values("date")
        .reset_index(drop=True)
    )
    travel = build_travel_tables(od_reference, zone_ids)
    return {
        "zone_ids": zone_ids,
        "zones": zones,
        "adjacent": build_adjacent_matrix(neighbour_edges, zone_ids),
        "mean_toll_usd": mean_toll_usd,
        "hourly_requests": build_hourly_requests(zone_hour, zone_ids),
        "day_table": day_table,
        "training_zone_hour": training,
        **travel,
    }


def make_previous_week_forecast(inputs, day):
    """Forecast for a day = the actual requests of the same weekday one week earlier."""
    earlier_day = day - timedelta(days=DAYS_PER_WEEK)
    if earlier_day not in inputs["hourly_requests"]:
        raise KeyError(f"No actual requests for {earlier_day}, so no previous-week forecast.")
    return np.nan_to_num(inputs["hourly_requests"][earlier_day], nan=0.0)


def generate_day_requests(inputs, day, settings, seed):
    """Draw one day of simulated requests from the actual counts. Policy-independent.

    Every zone is generated (the same draws whatever the allowed zones are), so all policies
    and all launch areas see the same realised demand for the same seed and day.
    """
    rng = np.random.default_rng([seed, day.toordinal(), DEMAND_STREAM])
    actual = inputs["hourly_requests"][day]
    is_missing = np.isnan(actual)
    real_counts = np.nan_to_num(actual, nan=0.0).astype(np.int64)
    scaled_counts = rng.binomial(real_counts, settings["demand_scale"])
    slot_split = rng.multinomial(scaled_counts, [1 / SLOTS_PER_HOUR] * SLOTS_PER_HOUR)
    zone_count = len(inputs["zone_ids"])
    counts_by_slot_zone = slot_split.transpose(0, 2, 1).reshape(SLOTS_PER_DAY, zone_count)
    flat_position = np.repeat(np.arange(SLOTS_PER_DAY * zone_count), counts_by_slot_zone.ravel())
    slot = flat_position // zone_count
    origin = flat_position % zone_count
    destination = (rng.random(len(origin))[:, None] > inputs["destination_cdf"][origin]).sum(axis=1)
    destination = np.minimum(destination, zone_count - 1)
    order = np.lexsort((rng.random(len(origin)), slot))
    slot, origin, destination = slot[order], origin[order], destination[order]
    return {
        "day": day,
        "slot": slot,
        "origin": origin,
        "destination": destination,
        "trip_miles": inputs["miles_matrix"][origin, destination],
        "trip_minutes": inputs["minutes_matrix"][origin, destination],
        "toll_usd": inputs["mean_toll_usd"][origin],
        "missing_zone_hours": is_missing,
    }


def split_cars_by_weights(car_count, weights):
    """Split car_count whole cars in proportion to weights (largest remainder, deterministic)."""
    weights = np.asarray(weights, dtype=float)
    if weights.sum() <= 0:
        weights = (weights >= 0).astype(float)
    exact = car_count * weights / weights.sum()
    cars = np.floor(exact).astype(int)
    leftover = car_count - cars.sum()
    by_largest_remainder = np.argsort(-(exact - cars), kind="stable")
    cars[by_largest_remainder[:leftover]] += 1
    return cars


def plan_moves_to_targets(idle_by_zone, target_by_zone, miles_matrix):
    """List (from_zone, to_zone) moves that turn idle_by_zone into target_by_zone.

    Simple greedy: each missing car is taken from the nearest zone that has a spare car.
    """
    spare_zones = list(np.repeat(np.arange(len(idle_by_zone)), np.maximum(idle_by_zone - target_by_zone, 0)))
    missing_zones = np.repeat(np.arange(len(idle_by_zone)), np.maximum(target_by_zone - idle_by_zone, 0))
    moves = []
    for to_zone in missing_zones:
        distances = miles_matrix[spare_zones, to_zone]
        from_zone = spare_zones.pop(int(np.argmin(distances)))
        moves.append((int(from_zone), int(to_zone)))
    return moves


def plan_profit_moves(idle_by_zone, expected_requests, profit_per_request_usd, is_allowed, move_cost_usd, settings):
    """Place idle cars one at a time where the next car adds the most expected profit.

    The value of the next car in a zone is (expected requests it can still take, at most
    requests_per_car_hour) x profit per request. A car that has to drive there pays the
    empty-drive cost. When no placement has a positive gain, the remaining cars stay.
    """
    cap = settings["requests_per_car_hour"]
    from_zones = np.flatnonzero(idle_by_zone > 0)
    to_zones = np.flatnonzero(is_allowed)
    cars_left = idle_by_zone[from_zones].copy()
    cars_placed = np.zeros(len(idle_by_zone))
    costs = move_cost_usd[np.ix_(from_zones, to_zones)]
    moves = []
    while cars_left.sum() > 0:
        next_requests = np.clip(expected_requests - cars_placed * cap, 0, cap)
        value = (next_requests * profit_per_request_usd)[to_zones]
        gain = value[None, :] - costs
        gain[cars_left == 0, :] = -np.inf
        best_row, best_column = np.unravel_index(np.argmax(gain), gain.shape)
        if gain[best_row, best_column] <= 0:
            break
        from_zone, to_zone = from_zones[best_row], to_zones[best_column]
        cars_left[best_row] -= 1
        cars_placed[to_zone] += 1
        if from_zone != to_zone:
            moves.append((int(from_zone), int(to_zone)))
    return moves


def choose_hourly_moves(policy, idle_by_zone, forecast_hour, context, rng):
    """Return the (from_zone, to_zone) moves a placement policy wants at an hour start."""
    is_allowed = context["is_allowed"]
    idle_count = int(idle_by_zone.sum())
    if policy == "ride_count":
        weights = np.where(is_allowed, forecast_hour, 0.0)
        targets = split_cars_by_weights(idle_count, weights)
    elif policy == "equal":
        targets = split_cars_by_weights(idle_count, is_allowed.astype(float))
    elif policy == "random":
        targets = rng.multinomial(idle_count, is_allowed / is_allowed.sum())
    elif policy == "profit":
        scaled_forecast = forecast_hour * context["settings"]["demand_scale"]
        expected_requests = scaled_forecast * context["servable_fraction"]
        return plan_profit_moves(
            idle_by_zone, expected_requests, context["profit_per_request_usd"],
            is_allowed, context["move_cost_usd"], context["settings"],
        )
    else:
        raise ValueError(f"{policy} is not an hourly placement policy.")
    return plan_moves_to_targets(idle_by_zone, targets, context["miles_matrix"])


def make_policy_context(inputs, is_allowed, settings):
    """Work out per-zone numbers the policies need: servable share, profit per request, move cost."""
    probability = np.diff(inputs["destination_cdf"], axis=1, prepend=0.0)
    allowed_probability = probability * is_allowed[None, :]
    servable_fraction = allowed_probability.sum(axis=1)
    miles = inputs["miles_matrix"]
    profit_per_trip = (
        settings["base_charge_usd"] + (settings["per_mile_rate_usd"] - settings["cost_per_mile_usd"]) * miles
    )
    expected_profit = (allowed_probability * profit_per_trip).sum(axis=1)
    profit_per_request_usd = np.where(
        servable_fraction > 0, expected_profit / np.maximum(servable_fraction, 1e-12), 0.0
    ) - inputs["mean_toll_usd"]
    return {
        "is_allowed": is_allowed,
        "servable_fraction": servable_fraction,
        "profit_per_request_usd": profit_per_request_usd,
        "move_cost_usd": settings["cost_per_mile_usd"] * miles,
        "miles_matrix": miles,
        "settings": settings,
    }


def drive_car_empty(fleet, tally, car, to_zone, start_minute, inputs, is_charged):
    """Send one car to another zone. If charged, it is busy for the drive and empty miles count."""
    from_zone = fleet["zone"][car]
    if is_charged:
        drive_minutes = inputs["minutes_matrix"][from_zone, to_zone]
        fleet["free_minute"][car] = start_minute + drive_minutes
        fleet["busy_minutes"][car] += min(fleet["free_minute"][car], DAY_MINUTES) - start_minute
        fleet["just_freed"][car] = False
        tally["empty_miles"] += inputs["miles_matrix"][from_zone, to_zone]
        tally["moved_cars"] += 1
    fleet["zone"][car] = to_zone


def apply_moves(fleet, tally, moves, now_minute, inputs, is_charged):
    """Carry out planned moves, always using a car that is idle now in the from-zone."""
    for from_zone, to_zone in moves:
        is_candidate = (fleet["free_minute"] <= now_minute) & (fleet["zone"] == from_zone)
        car = int(np.flatnonzero(is_candidate)[0])
        drive_car_empty(fleet, tally, car, to_zone, now_minute, inputs, is_charged)


def apply_wait_or_move_rule(fleet, tally, forecast_hour, context, now_minute, inputs):
    """Cars that just became free move to a clearly busier neighbour, otherwise they wait."""
    adjacent = inputs["adjacent"]
    move_ratio = context["settings"]["move_ratio"]
    just_freed_cars = np.flatnonzero(fleet["just_freed"] & (fleet["free_minute"] <= now_minute))
    for car in just_freed_cars:
        fleet["just_freed"][car] = False
        zone = fleet["zone"][car]
        neighbours = np.flatnonzero(adjacent[zone] & context["is_allowed"])
        if len(neighbours) == 0:
            continue
        best_neighbour = neighbours[np.argmax(forecast_hour[neighbours])]
        if forecast_hour[zone] < move_ratio * forecast_hour[best_neighbour]:
            drive_car_empty(fleet, tally, int(car), int(best_neighbour), now_minute, inputs, True)


def find_car_for_request(fleet, is_idle, zone, within_hops, miles_matrix):
    """Return the car for a request: same zone first, else nearest neighbour zone, else None."""
    same_zone_cars = np.flatnonzero(is_idle & (fleet["zone"] == zone))
    if len(same_zone_cars) > 0:
        return int(same_zone_cars[0])
    neighbour_cars = np.flatnonzero(is_idle & within_hops[zone][fleet["zone"]])
    if len(neighbour_cars) == 0:
        return None
    distances = miles_matrix[fleet["zone"][neighbour_cars], zone]
    return int(neighbour_cars[np.argmin(distances)])


def serve_request(fleet, tally, is_idle, request_number, requests, now_minute, within_hops, inputs):
    """Try to serve one request. Updates the car, the tally and the idle flags."""
    origin = requests["origin"][request_number]
    car = find_car_for_request(fleet, is_idle, origin, within_hops, inputs["miles_matrix"])
    if car is None:
        return
    pickup_zone = fleet["zone"][car]
    pickup_miles = inputs["miles_matrix"][pickup_zone, origin] if pickup_zone != origin else 0.0
    pickup_minutes = inputs["minutes_matrix"][pickup_zone, origin] if pickup_zone != origin else 0.0
    free_minute = now_minute + pickup_minutes + requests["trip_minutes"][request_number]
    fleet["busy_minutes"][car] += min(free_minute, DAY_MINUTES) - now_minute
    fleet["free_minute"][car] = free_minute
    fleet["zone"][car] = requests["destination"][request_number]
    fleet["just_freed"][car] = True
    is_idle[car] = False
    tally["served"][origin] += 1
    tally["passenger_miles"] += requests["trip_miles"][request_number]
    tally["tolls_usd"] += requests["toll_usd"][request_number]
    tally["empty_miles"] += pickup_miles


def keep_requests_inside_area(requests, is_allowed):
    """Keep only requests that start and end in allowed zones (the geofence rule)."""
    keep = is_allowed[requests["origin"]] & is_allowed[requests["destination"]]
    request_columns = ["slot", "origin", "destination", "trip_miles", "trip_minutes", "toll_usd"]
    return {name: requests[name][keep] for name in request_columns}


def run_day(inputs, requests, forecast_hour_by_hour, policy, n_cars, is_allowed, settings, seed):
    """Replay the 96 slots of one day for one policy. Returns the fleet state and tallies.

    forecast_hour_by_hour is the scaled-to-real (24, zones) forecast the policy may look at.
    Requests that start in a disallowed zone or end outside the allowed zones are dropped first.
    """
    zone_count = len(inputs["zone_ids"])
    rng = np.random.default_rng([seed, requests["day"].toordinal(), POLICY_STREAM])
    context = make_policy_context(inputs, is_allowed, settings)
    within_hops = build_within_hops_matrix(inputs["adjacent"], settings["max_pickup_hops"])
    kept = keep_requests_inside_area(requests, is_allowed)
    first_hour_weights = np.where(is_allowed, forecast_hour_by_hour[0], 0.0)
    start_cars = split_cars_by_weights(n_cars, first_hour_weights)
    fleet = {
        "zone": np.repeat(np.arange(zone_count), start_cars),
        "free_minute": np.zeros(n_cars),
        "busy_minutes": np.zeros(n_cars),
        "just_freed": np.zeros(n_cars, dtype=bool),
    }
    tally = {
        "served": np.zeros(zone_count, dtype=int),
        "passenger_miles": 0.0,
        "tolls_usd": 0.0,
        "empty_miles": 0.0,
        "moved_cars": 0,
    }
    slot_starts = np.searchsorted(kept["slot"], np.arange(SLOTS_PER_DAY + 1))
    for slot in range(SLOTS_PER_DAY):
        now_minute = slot * SLOT_MINUTES + REQUEST_MINUTE_IN_SLOT
        hour = slot // SLOTS_PER_HOUR
        if slot % SLOTS_PER_HOUR == 0 and policy in HOURLY_PLACEMENT_POLICIES:
            is_idle_now = fleet["free_minute"] <= now_minute
            idle_by_zone = np.bincount(fleet["zone"][is_idle_now], minlength=zone_count)
            moves = choose_hourly_moves(policy, idle_by_zone, forecast_hour_by_hour[hour], context, rng)
            apply_moves(fleet, tally, moves, now_minute, inputs, is_charged=slot > 0)
        if policy == "wait_or_move":
            apply_wait_or_move_rule(fleet, tally, forecast_hour_by_hour[hour], context, now_minute, inputs)
        is_idle = fleet["free_minute"] <= now_minute
        for request_number in range(slot_starts[slot], slot_starts[slot + 1]):
            serve_request(fleet, tally, is_idle, request_number, kept, now_minute, within_hops, inputs)
    return fleet, tally, kept


def summarise_day(inputs, requests, kept, fleet, tally, forecast_hour_by_hour, policy, n_cars, is_allowed, settings):
    """Turn the raw tallies into one summary row (a dict) and a per-zone table."""
    zone_count = len(inputs["zone_ids"])
    served = int(tally["served"].sum())
    requests_by_zone = np.bincount(kept["origin"], minlength=zone_count)
    started_inside = int(is_allowed[requests["origin"]].sum())
    revenue_usd = served * settings["base_charge_usd"] + settings["per_mile_rate_usd"] * tally["passenger_miles"]
    all_miles = tally["passenger_miles"] + tally["empty_miles"]
    mile_cost_usd = settings["cost_per_mile_usd"] * all_miles
    idle_car_hours = (n_cars * DAY_MINUTES - fleet["busy_minutes"].sum()) / MINUTES_PER_HOUR
    idle_cost_usd = settings["idle_cost_per_car_hour_usd"] * idle_car_hours
    profit_usd = revenue_usd - tally["tolls_usd"] - mile_cost_usd - idle_cost_usd
    zone_table = pd.DataFrame({
        "zone_id": inputs["zone_ids"],
        "requests": requests_by_zone,
        "served": tally["served"],
        "forecast_requests_per_day": forecast_hour_by_hour.sum(axis=0),
    })
    zone_table = zone_table[is_allowed].reset_index(drop=True)
    zone_table["served_share"] = zone_table["served"] / zone_table["requests"].where(zone_table["requests"] > 0)
    is_high = zone_table["forecast_requests_per_day"] >= zone_table["forecast_requests_per_day"].median()
    high_share = zone_table[is_high]["served"].sum() / max(zone_table[is_high]["requests"].sum(), 1)
    low_share = zone_table[~is_high]["served"].sum() / max(zone_table[~is_high]["requests"].sum(), 1)
    summary = {
        "date": requests["day"],
        "policy": policy,
        "n_cars": n_cars,
        "requests_all_zones": len(requests["origin"]),
        "requests_in_area": started_inside,
        "requests_dropped_outside": started_inside - len(kept["origin"]),
        "served": served,
        "served_share": served / started_inside if started_inside else np.nan,
        "passenger_miles": tally["passenger_miles"],
        "empty_miles": tally["empty_miles"],
        "empty_mile_share": tally["empty_miles"] / all_miles if all_miles else np.nan,
        "moved_cars": tally["moved_cars"],
        "revenue_usd": revenue_usd,
        "tolls_usd": tally["tolls_usd"],
        "mile_cost_usd": mile_cost_usd,
        "idle_cost_usd": idle_cost_usd,
        "profit_usd": profit_usd,
        "profit_per_car_usd": profit_usd / n_cars,
        "idle_car_hours": idle_car_hours,
        "served_share_high_demand_zones": high_share,
        "served_share_low_demand_zones": low_share,
        "service_gap": high_share - low_share,
        "lowest_zone_served_share": zone_table["served_share"].min(),
        "missing_zone_hours_in_area": int(requests["missing_zone_hours"][:, is_allowed].sum()),
    }
    return summary, zone_table


def make_allowed_mask(inputs, allowed_zone_ids):
    """Turn a list of zone ids into a True/False array over the simulator's zones."""
    is_allowed = np.isin(inputs["zone_ids"], list(allowed_zone_ids))
    if is_allowed.sum() != len(set(allowed_zone_ids)):
        raise ValueError("Some allowed zone ids are not simulator zones.")
    return is_allowed


def check_forecast(forecast_hour_by_hour, zone_count):
    """Stop with a clear error if a forecast has the wrong shape or invalid numbers."""
    forecast = np.asarray(forecast_hour_by_hour, dtype=float)
    if forecast.shape != (HOURS_PER_DAY, zone_count):
        raise ValueError(f"Forecast must have shape {(HOURS_PER_DAY, zone_count)}, got {forecast.shape}.")
    if not np.isfinite(forecast).all() or (forecast < 0).any():
        raise ValueError("Forecast must be finite and not negative.")
    return forecast


def simulate_day(inputs, requests, forecast_hour_by_hour, policy, n_cars, allowed_zone_ids, settings,
                 seed=DEFAULT_SEED):
    """Simulate one day for one policy. Returns (summary_row_dict, per_zone_table).

    requests comes from generate_day_requests, so every policy can share the same demand.
    """
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy {policy!r}; choose from {POLICIES}.")
    if n_cars < 1:
        raise ValueError("The fleet needs at least one car.")
    is_allowed = make_allowed_mask(inputs, allowed_zone_ids)
    forecast = check_forecast(forecast_hour_by_hour, len(inputs["zone_ids"]))
    fleet, tally, kept = run_day(inputs, requests, forecast, policy, n_cars, is_allowed, settings, seed)
    return summarise_day(inputs, requests, kept, fleet, tally, forecast, policy, n_cars, is_allowed, settings)


def simulate_days(inputs, days, forecast_by_date, policies, n_cars, allowed_zone_ids, settings,
                  seed=DEFAULT_SEED):
    """Simulate many days and policies. Returns a DataFrame with one row per (date, policy).

    All policies on a date share the same realised demand. Everything is deterministic
    given the seed.
    """
    rows = []
    for day in days:
        requests = generate_day_requests(inputs, day, settings, seed)
        for policy in policies:
            summary, _ = simulate_day(
                inputs, requests, forecast_by_date[day], policy, n_cars, allowed_zone_ids, settings, seed
            )
            rows.append(summary)
    return pd.DataFrame(rows)
