"""Phase 7 (optional): record the frozen forecast run in MLflow, a local experiment log.

Where it fits: it does not train or change anything. It only copies the numbers the team already
saved (data/forecasts/frozen_choice.json, forecast_summary.json and, if present,
data/drift/drift_summary.json) into a local MLflow run, so settings, scores and files of the frozen
choice sit in one searchable place. Runs are written to the git-ignored folder mlruns/.
Nov-Dec 2024 was examined by the team before, so its scores are tagged "previously examined".
Run from anywhere inside the repo (needs `pip install mlflow-skinny`), then optionally `mlflow ui`:
    python notebooks/07_mlflow_tracking.py
"""

import json
import os
import re
from pathlib import Path

import mlflow

# MLflow 3 warns that its plain-folder store is in maintenance mode. We still want a simple mlruns/
# folder with no database, so we say so explicitly (this only affects this script).
os.environ["MLFLOW_ALLOW_FILE_STORE"] = "true"

REPO_ROOT = Path(__file__).resolve().parents[1]
FORECAST_FOLDER = REPO_ROOT / "data/forecasts"
DRIFT_FOLDER = REPO_ROOT / "data/drift"
TRACKING_FOLDER = REPO_ROOT / "mlruns"
EXPERIMENT_NAME = "robotaxi-frozen-forecast"
RUN_NAME = "frozen-choice-phase3"
SCORE_NAMES = ["MAE", "RMSE", "WAPE_percent"]
SUMMARY_SCORE_SECTIONS = {
    "september": "september_scores",
    "oct16_31_selection": "selection_scores_oct_16_31",
    "nov_dec_previously_examined": "nov_dec_overall_scores",
}
ARTIFACT_FILES = [
    FORECAST_FOLDER / "frozen_choice.json",
    FORECAST_FOLDER / "forecast_summary.json",
    FORECAST_FOLDER / "frozen_model.joblib",
    DRIFT_FOLDER / "drift_summary.json",
]


def make_metric_name(section, label, score_name):
    """Build a metric name such as september_hgb_squared_small_MAE (letters, digits and underscores only)."""
    clean_label = re.sub(r"[^a-z0-9]+", "_", str(label).lower()).strip("_")
    return f"{section}_{clean_label}_{score_name}"


def read_json(path):
    """Read a JSON file; stop with a clear error if it is missing."""
    if not path.is_file():
        raise FileNotFoundError(f"Missing file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def collect_params(choice):
    """Return the settings that define the frozen choice (all plain values)."""
    params = {f"setting_{name}": value for name, value in choice["chosen_settings"].items()}
    params["chosen_candidate"] = choice["chosen_candidate"]
    params["random_seed"] = choice["random_seed"]
    params["feature_count"] = len(choice["feature_columns"])
    params["count_bias_scale"] = choice["count_bias_scale"]
    params["pred_model_uses_scale"] = choice["pred_model_uses_scale"]
    params["frozen_profile_end_date"] = choice["frozen_profile_end_date"]
    params["best_method_on_oct_16_31"] = choice["best_method_on_oct_16_31"]
    return params


def collect_score_metrics(summary):
    """Return every saved score table as a flat dictionary of metric name -> number."""
    metrics = {}
    for section, key in SUMMARY_SCORE_SECTIONS.items():
        for row in summary[key]:
            label = row.get("candidate", row.get("method"))
            for score_name in SCORE_NAMES:
                metrics[make_metric_name(section, label, score_name)] = float(row[score_name])
    return metrics


def collect_drift_metrics(drift_summary):
    """Return the headline drift numbers (PSI by feature for January 2025 and the error monitor)."""
    metrics = {}
    for row in drift_summary["data_drift"]:
        if row["comparison"] == "drift_jan_2025":
            metrics[f"drift_psi_{row['feature']}"] = float(row["psi"])
    error = drift_summary["error_monitor_frozen_model"]
    metrics["drift_validation_wape_percent"] = float(error["validation_wape_percent"])
    metrics["drift_january_max_weekly_wape_percent"] = float(error["january_max_weekly_wape_percent"])
    metrics["drift_weeks_over_limit_after_october"] = float(len(error["weeks_over_limit_after_october"]))
    return metrics


def main():
    """Create one MLflow run for the frozen choice and print where it was saved."""
    choice = read_json(FORECAST_FOLDER / "frozen_choice.json")
    summary = read_json(FORECAST_FOLDER / "forecast_summary.json")
    mlflow.set_tracking_uri(TRACKING_FOLDER.as_uri())
    mlflow.set_experiment(EXPERIMENT_NAME)
    with mlflow.start_run(run_name=RUN_NAME):
        params = collect_params(choice)
        mlflow.log_params(params)
        mlflow.set_tags({"nov_dec_status": summary["nov_dec_status"],
                         "january_2025_status": summary["january_2025_status"]})
        metrics = collect_score_metrics(summary)
        drift_path = DRIFT_FOLDER / "drift_summary.json"
        if drift_path.is_file():
            metrics.update(collect_drift_metrics(read_json(drift_path)))
        mlflow.log_metrics(metrics)
        for path in ARTIFACT_FILES:
            if path.is_file():
                mlflow.log_artifact(str(path))
    print(f"Logged {len(metrics)} metrics and {len(params)} parameters to {TRACKING_FOLDER}")


if __name__ == "__main__":
    main()
