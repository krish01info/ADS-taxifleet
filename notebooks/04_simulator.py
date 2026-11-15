"""Phase 4 demo: simulate two test-period days with the fleet simulator and print the results.

Where it fits: this is a quick check that src/fleet_sim.py runs end to end on the real release.
It uses a naive forecast (the same weekday one week earlier) and a whole-city launch area.
Nov 4-5 2024 are in the test period, which the team has already inspected, so these numbers
are a demo of the machinery and are previously examined, not pristine. Run from the repo root:
    python notebooks/04_simulator.py
"""

import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import fleet_sim

DEMO_DAYS = [date(2024, 11, 4), date(2024, 11, 5)]
DEMO_FLEET_SIZE = 250
DEMO_SEED = 1
OUTPUT_FOLDER_NAME = Path("data") / "simulation"
OUTPUT_FILE_NAME = "demo_two_days.csv"
SHOWN_COLUMNS = [
    "date", "policy", "requests_in_area", "served_share", "empty_mile_share", "revenue_usd",
    "profit_usd", "profit_per_car_usd", "idle_car_hours", "moved_cars", "service_gap",
]


def main():
    """Load the release, fit the price, run all policies on the demo days, print and save."""
    inputs = fleet_sim.load_simulation_inputs()
    price = fleet_sim.fit_common_price(inputs["training_zone_hour"])
    print("Common price fitted on training zone-hours:")
    print({name: round(value, 4) for name, value in price.items()})
    print(f"Detour factor {inputs['detour_factor']:.3f}, average speed {inputs['average_speed_mph']:.1f} mph")
    settings = fleet_sim.make_settings(price["base_charge_usd"], price["per_mile_rate_usd"])
    forecast_by_date = {day: fleet_sim.make_previous_week_forecast(inputs, day) for day in DEMO_DAYS}
    started = time.time()
    results = fleet_sim.simulate_days(
        inputs, DEMO_DAYS, forecast_by_date, fleet_sim.POLICIES, DEMO_FLEET_SIZE,
        inputs["zone_ids"], settings, seed=DEMO_SEED,
    )
    seconds_per_day_policy = (time.time() - started) / len(results)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    print(results[SHOWN_COLUMNS].round(3).to_string(index=False))
    print(f"Runtime: {seconds_per_day_policy:.3f} seconds per day and policy")
    output_folder = fleet_sim.find_repo_root() / OUTPUT_FOLDER_NAME
    output_folder.mkdir(parents=True, exist_ok=True)
    results.to_csv(output_folder / OUTPUT_FILE_NAME, index=False)


if __name__ == "__main__":
    main()
