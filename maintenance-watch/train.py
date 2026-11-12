"""Reproduce the supplied notebook and export a versioned scoring artifact."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn import __version__ as sklearn_version
from sklearn.compose import ColumnTransformer
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

from scoring import FEATURES, FEATURE_ORDER, REVIEW_THRESHOLD, FailureModel, model_version


ROOT = Path(__file__).resolve().parent
NUMERIC_COLUMNS = [column for column, _, _ in FEATURES.values()]
RAW_COLUMNS = [*NUMERIC_COLUMNS, "Type"]
TARGET = "Machine failure"
TREE_SETTINGS = {
    "criterion": "gini",
    "max_depth": 10,
    "min_samples_leaf": 1,
    "min_samples_split": 10,
    "random_state": 42,
}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def reading_features(row):
    return {"grade": row["Type"], **{name: float(row[column]) for name, (column, _, _) in FEATURES.items()}}


def build_replay(holdout, path):
    normal = holdout[holdout[TARGET] == 0].sample(frac=1, random_state=91)
    failed = holdout[holdout[TARGET] == 1].sample(frac=1, random_state=92)
    normal_rows = iter(normal.to_dict(orient="records"))
    failed_rows = iter(failed.to_dict(orient="records"))
    columns = ["source_udi", "asset_id", "observed_failure", "grade", *FEATURES]
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for index in range(360):
            row = next(failed_rows if index % 13 == 7 else normal_rows)
            writer.writerow({
                "source_udi": int(row["UDI"]),
                "asset_id": f"CNC-{index % 3 + 1:02d}",
                "observed_failure": int(row[TARGET]),
                **reading_features(row),
            })
    temporary.replace(path)


def export_model(pipeline, path):
    scaler = pipeline.named_steps["scale"]
    tree = pipeline.named_steps["tree"].tree_
    nodes = []
    for index, left in enumerate(tree.children_left):
        if left == -1:
            values = tree.value[index, 0]
            nodes.append({"score": float(values[1] / values.sum())})
        else:
            nodes.append({
                "feature": int(tree.feature[index]),
                "threshold": float(tree.threshold[index]),
                "left": int(left),
                "right": int(tree.children_right[index]),
            })
    payload = {"features": FEATURE_ORDER, "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(), "nodes": nodes}
    version = model_version(payload)
    write_json(path, {**payload, "model_version": version})
    return version


def train(csv_path, artifacts, replay_path):
    data = pd.read_csv(csv_path)
    if len(data) != 10_000 or sorted(data[TARGET].unique()) != [0, 1]:
        raise ValueError("Expected the 10,000-row UCI AI4I CSV with binary Machine failure labels.")
    training, holdout = train_test_split(data, test_size=0.2, random_state=42)
    encoder = ColumnTransformer([
        ("sensors", "passthrough", NUMERIC_COLUMNS),
        ("grade", OneHotEncoder(categories=[["H", "L", "M"]], drop="first", sparse_output=False), ["Type"]),
    ])
    pipeline = Pipeline([
        ("encode", encoder),
        ("scale", StandardScaler()),
        ("tree", DecisionTreeClassifier(**TREE_SETTINGS)),
    ])
    pipeline.fit(training[RAW_COLUMNS], training[TARGET])
    scores = pipeline.predict_proba(holdout[RAW_COLUMNS])[:, 1]
    predictions = scores > REVIEW_THRESHOLD
    truth = holdout[TARGET]
    tn, fp, fn, tp = (int(item) for item in confusion_matrix(truth, predictions, labels=[0, 1]).ravel())
    artifacts.mkdir(parents=True, exist_ok=True)
    version = export_model(pipeline, artifacts / "model.json")
    portable = FailureModel(artifacts / "model.json")
    portable_scores = [portable.score(reading_features(row)) for row in holdout.to_dict(orient="records")]
    np.testing.assert_allclose(portable_scores, scores, rtol=0, atol=1e-12)
    validation = {
        "model_version": version,
        "dataset": "UCI AI4I 2020 (synthetic)",
        "dataset_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "sklearn": sklearn_version,
        "split": {"training_rows": len(training), "holdout_rows": len(holdout), "seed": 42, "method": "notebook random 80/20 split"},
        "threshold": REVIEW_THRESHOLD,
        "accuracy": accuracy_score(truth, predictions),
        "failure_precision": precision_score(truth, predictions),
        "failure_recall": recall_score(truth, predictions),
        "failure_f1": f1_score(truth, predictions),
        "average_precision": average_precision_score(truth, scores),
        "confusion": {"true_negative": tn, "false_positive": fp, "false_negative": fn, "true_positive": tp},
        "portable_parity_rows": len(holdout),
    }
    write_json(artifacts / "validation.json", validation)
    build_replay(holdout, replay_path)
    return pipeline, validation, training[RAW_COLUMNS].head(3)


def log_to_mlflow(pipeline, validation, example, artifacts):
    import mlflow
    import mlflow.sklearn

    mlflow.set_experiment("Machine_Failure_Prediction")
    with mlflow.start_run(run_name=validation["model_version"]):
        mlflow.log_params(TREE_SETTINGS)
        mlflow.log_metrics({key: validation[key] for key in ("accuracy", "failure_precision", "failure_recall", "failure_f1", "average_precision")})
        mlflow.log_artifacts(str(artifacts), artifact_path="monitoring")
        mlflow.sklearn.log_model(sk_model=pipeline, name="maintenance_pipeline", input_example=example)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "data" / "ai4i2020.csv")
    parser.add_argument("--mlflow", action="store_true", help="Log the full pipeline and portable model to the configured MLflow server.")
    args = parser.parse_args()
    artifacts = ROOT / "artifacts"
    pipeline, validation, example = train(args.csv, artifacts, ROOT / "data" / "replay.csv")
    if args.mlflow:
        log_to_mlflow(pipeline, validation, example, artifacts)
    print(json.dumps(validation, indent=2))


if __name__ == "__main__":
    main()
