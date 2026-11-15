"""Phase 5 runner: find proxy-optimal launch areas and compare them with the fixed baseline.

Run from anywhere:  python notebooks/05_launch_area.py
Steps: load TRAINING data -> build the zone graph (three bridge-link settings) -> print
the connected groups -> score the fixed baseline -> search for the best connected area
of several sizes -> save tables and a map to data/launch_area/.

Only training data (Jan-Aug 2024) is used. Validation and test months are never read.
The main result uses the graph with river-crossing links; the other two graphs
(no bridge links, and river plus airport-road links) are sensitivity checks.
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.launch_area import (
    BRIDGE_LINKS,
    LINK_TYPE_AIRPORT_ROAD,
    LINK_TYPE_RIVER_CROSSING,
    area_is_connected,
    bridge_links_table,
    build_proxy_inputs,
    build_zone_graph,
    count_new_edges,
    demand_share_of_area,
    find_connected_groups,
    find_repo_root,
    fixed_baseline_area,
    grow_launch_area,
    load_polygon_neighbours,
    load_training_mean_hourly_requests,
    load_training_zone_pairs,
    load_zones,
    make_proxy_score,
    make_zone_demand_share,
    pick_top_demand_seeds,
    read_parquet_directory,
    release_table_directory,
    select_bridge_links,
)

OUTPUT_DIRECTORY_NAME = "data/launch_area"
AREA_SIZES = (15, 30, 60)
NUMBER_OF_SEED_ZONES = 5
MIN_DEMAND_SHARE = 0.10
MAIN_GRAPH_NAME = "river_links"
BASELINE_LABEL = "fixed_baseline"
FIGURE_DPI = 130
FIGURE_FILE_NAME = "launch_areas_map.png"
MAP_COLUMNS = 3


def make_graphs(zones, polygon_neighbours):
    """The three zone graphs: no bridge links, river links only, river plus airport links."""
    link_settings = {
        "no_links": None,
        MAIN_GRAPH_NAME: select_bridge_links([LINK_TYPE_RIVER_CROSSING]),
        "river_and_airport_links": select_bridge_links(
            [LINK_TYPE_RIVER_CROSSING, LINK_TYPE_AIRPORT_ROAD]),
    }
    return {name: build_zone_graph(zones, polygon_neighbours, links)
            for name, links in link_settings.items()}


def describe_groups(graph_name, graph, zones):
    """One table row per connected group of a graph: size, boroughs and, if small, zone names."""
    names = dict(zip(zones["zone_id"], zones["zone_name"]))
    boroughs = dict(zip(zones["zone_id"], zones["borough"]))
    rows = []
    for group_number, group in enumerate(find_connected_groups(graph), start=1):
        group_boroughs = sorted({boroughs[zone_id] for zone_id in group})
        small_group_names = "; ".join(names[zone_id] for zone_id in sorted(group))
        rows.append({"graph": graph_name, "group_number": group_number, "zone_count": len(group),
                     "boroughs": ", ".join(group_boroughs),
                     "zone_names_if_small": small_group_names if len(group) <= 5 else ""})
    return pd.DataFrame(rows)


def summarise_area(graph_name, area_label, zone_ids, zones, zone_demand_share, score, graphs,
                   score_calls):
    """One summary row for an area: size, boroughs, demand share, proxy score, connectivity."""
    area_zones = zones[zones["zone_id"].isin(zone_ids)]
    borough_counts = area_zones["borough"].value_counts()
    row = {"graph": graph_name, "area_label": area_label, "zone_count": len(zone_ids),
           "boroughs": ", ".join(f"{borough} {count}" for borough, count in borough_counts.items()),
           "training_demand_share": demand_share_of_area(zone_ids, zone_demand_share),
           "proxy_profit_per_car_hour_usd": score, "score_calls": score_calls}
    for name, graph in graphs.items():
        row[f"connected_in_{name}"] = area_is_connected(graph, zone_ids)
    return row


def area_rows(area_label, zone_ids, zones, graph_name=None):
    """Rows of launch_areas.csv for one area: zone_id, zone_name, borough, area_label."""
    rows = zones.loc[zones["zone_id"].isin(zone_ids), ["zone_id", "zone_name", "borough"]].copy()
    rows["area_label"] = area_label
    if graph_name is not None:
        rows["graph"] = graph_name
    return rows


def draw_area_map(axes, zone_polygons, zone_ids, title):
    """Draw every zone in grey and the chosen zones in colour on one map panel."""
    chosen = set(zone_ids)
    for zone_id, polygon in zone_polygons.items():
        colour = "tab:orange" if zone_id in chosen else "lightgrey"
        parts = polygon.geoms if polygon.geom_type == "MultiPolygon" else [polygon]
        for part in parts:
            x_values, y_values = part.exterior.xy
            axes.fill(x_values, y_values, facecolor=colour, edgecolor="white", linewidth=0.3)
    axes.set_title(title, fontsize=9)
    axes.set_aspect("equal")
    axes.axis("off")


def save_map(repo_root, areas_to_draw, output_directory):
    """Save one figure with a map panel per area (areas_to_draw: label -> zone id list)."""
    zone_directory = release_table_directory(repo_root, "zones")
    geometry_table = read_parquet_directory(zone_directory, "zone_id, geometry_wkb")
    zone_polygons = {int(row.zone_id): shapely.from_wkb(bytes(row.geometry_wkb))
                     for row in geometry_table.itertuples()}
    panel_count = len(areas_to_draw)
    row_count = (panel_count + MAP_COLUMNS - 1) // MAP_COLUMNS
    figure, all_axes = plt.subplots(row_count, MAP_COLUMNS, figsize=(5 * MAP_COLUMNS, 5 * row_count))
    all_axes = all_axes.flatten()
    for axes, (label, zone_ids) in zip(all_axes, areas_to_draw.items()):
        draw_area_map(axes, zone_polygons, zone_ids, f"{label} ({len(zone_ids)} zones)")
    for unused_axes in all_axes[panel_count:]:
        unused_axes.axis("off")
    figure.suptitle("Launch areas (orange) chosen with the training-data proxy score")
    figure.tight_layout()
    figure.savefig(output_directory / FIGURE_FILE_NAME, dpi=FIGURE_DPI)
    plt.close(figure)


def main():
    repo_root = find_repo_root()
    output_directory = repo_root / OUTPUT_DIRECTORY_NAME
    output_directory.mkdir(parents=True, exist_ok=True)

    zones = load_zones(repo_root)
    polygon_neighbours = load_polygon_neighbours(repo_root)
    mean_hourly_requests = load_training_mean_hourly_requests(repo_root)
    zone_pairs = load_training_zone_pairs(repo_root)
    print(f"zones {len(zones)}, polygon neighbour rows {len(polygon_neighbours)}, "
          f"zones with training demand {len(mean_hourly_requests)}, "
          f"training zone pairs {len(zone_pairs)}")
    print(f"training hours counted per zone: {int(mean_hourly_requests['hours_counted'].min())} "
          f"to {int(mean_hourly_requests['hours_counted'].max())}")

    bridge_links_table().to_csv(output_directory / "bridge_links.csv", index=False)
    graphs = make_graphs(zones, polygon_neighbours)
    group_tables = [describe_groups(name, graph, zones) for name, graph in graphs.items()]
    pd.concat(group_tables).to_csv(output_directory / "connected_groups.csv", index=False)
    print(f"\nbridge links in the list: {len(BRIDGE_LINKS)}")
    for name, graph in graphs.items():
        groups = find_connected_groups(graph)
        print(f"graph {name}: {len(groups)} connected groups, sizes {[len(g) for g in groups]}, "
              f"new edges vs no_links: {count_new_edges(graphs['no_links'], graph)}")

    baseline_ids = fixed_baseline_area(zones)
    for name, graph in graphs.items():
        print(f"fixed baseline ({len(baseline_ids)} zones) connected in {name}: "
              f"{area_is_connected(graph, baseline_ids)}")

    proxy_score = make_proxy_score(build_proxy_inputs(zones, mean_hourly_requests, zone_pairs))
    zone_demand_share = make_zone_demand_share(mean_hourly_requests)
    seed_zone_ids = pick_top_demand_seeds(zone_demand_share, NUMBER_OF_SEED_ZONES)
    print(f"\nseed zones (highest training demand): {seed_zone_ids}")

    sizes = list(AREA_SIZES) + [len(baseline_ids)]
    summary_rows = [summarise_area(MAIN_GRAPH_NAME, BASELINE_LABEL, baseline_ids, zones,
                                   zone_demand_share, proxy_score(baseline_ids), graphs, 0)]
    main_area_tables = [area_rows(BASELINE_LABEL, baseline_ids, zones)]
    sensitivity_tables = []
    areas_to_draw = {BASELINE_LABEL: baseline_ids}
    for graph_name, graph in graphs.items():
        for size in sizes:
            label = f"proxy_optimal_{size}"
            result = grow_launch_area(graph, proxy_score, size, size, seed_zone_ids,
                                      zone_demand_share, MIN_DEMAND_SHARE)
            summary_rows.append(summarise_area(graph_name, label, result["zone_ids"], zones,
                                               zone_demand_share, result["score"], graphs,
                                               result["score_calls"]))
            print(f"{graph_name} {label}: score {result['score']:.3f} USD per car-hour, "
                  f"demand share {result['demand_share']:.3f}, seed {result['seed_zone_id']}")
            if graph_name == MAIN_GRAPH_NAME:
                main_area_tables.append(area_rows(label, result["zone_ids"], zones))
                areas_to_draw[label] = result["zone_ids"]
            else:
                sensitivity_tables.append(area_rows(label, result["zone_ids"], zones, graph_name))

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(output_directory / "launch_areas_summary.csv", index=False)
    pd.concat(main_area_tables).to_csv(output_directory / "launch_areas.csv", index=False)
    pd.concat(sensitivity_tables).to_csv(output_directory / "launch_areas_sensitivity.csv",
                                         index=False)
    save_map(repo_root, areas_to_draw, output_directory)
    pd.set_option("display.width", 220, "display.max_columns", 20)
    print("\n" + summary.to_string(index=False))
    print(f"\nSaved results to {output_directory}")


if __name__ == "__main__":
    main()
