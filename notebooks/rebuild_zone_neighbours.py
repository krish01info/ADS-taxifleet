"""Rebuild the corrected zone neighbour table from the official TLC zone files.

Fits into Phase 1 as a small stand-alone step: the released `zone_neighbours` table missed
305 real neighbouring pairs (see docs/decisions.md, "Zone neighbour table correction").
This script recomputes the table with `find_zone_neighbours` and writes it as Parquet, so
the corrected table can be rebuilt from the raw zone files by anyone.

Usage (run from the repository root, with the Phase 1 environment that has pyshp, shapely,
pyproj and pyarrow):
    python notebooks/rebuild_zone_neighbours.py --lookup taxi_zone_lookup.csv \
        --shapefile taxi_zones/taxi_zones.shp --output data/processed/phase1/zone_neighbours-fixed
"""

import argparse
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.phase1_sources import build_zone_metadata  # noqa: E402

EXPECTED_ZONES = 263
EDGE_SCHEMA = pa.schema([
    ("zone_id", pa.int64()), ("neighbour_zone_id", pa.int64()),
    ("shares_boundary", pa.bool_()), ("touches_at_point", pa.bool_()),
    ("shared_boundary_metres", pa.float64()),
])


def write_neighbour_table(lookup_path, shapefile_path, output_folder):
    """Build the neighbour edges from the zone files and save them; return the edge list."""
    zones, edges = build_zone_metadata(lookup_path, shapefile_path)
    if len(zones) != EXPECTED_ZONES:
        raise ValueError(f"Expected {EXPECTED_ZONES} zones, found {len(zones)}")
    edges = sorted(edges, key=lambda edge: (edge["zone_id"], edge["neighbour_zone_id"]))
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=False)
    pq.write_table(pa.Table.from_pylist(edges, schema=EDGE_SCHEMA), output_folder / "part-0.parquet")
    return edges


def main():
    """Read the command line, rebuild the table and print its size."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lookup", required=True, help="taxi_zone_lookup.csv")
    parser.add_argument("--shapefile", required=True, help="taxi_zones.shp (unzipped taxi_zones.zip)")
    parser.add_argument("--output", required=True, help="new folder for the Parquet table")
    arguments = parser.parse_args()
    edges = write_neighbour_table(arguments.lookup, arguments.shapefile, arguments.output)
    pairs = len(edges) // 2
    shared = sum(1 for edge in edges if edge["shares_boundary"]) // 2
    print(f"Wrote {len(edges)} directed edges = {pairs} neighbouring pairs "
          f"({shared} shared borders, {pairs - shared} corner contacts) to {arguments.output}")


if __name__ == "__main__":
    main()
