"""Rebuild the pump telemetry, score it, and update the Power BI import snapshot."""

from __future__ import annotations

import base64
import json
import textwrap
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import f1_score
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_DEFINITION = PROJECT_ROOT / "Pump_Maintenance.SemanticModel" / "definition"
FEATURES = ["vibration_mm_s", "temperature_c", "pressure_bar", "motor_current_amp"]
FAULTS = ["Bearing wear", "Overheating", "Cavitation"]
REVIEW_CUES = {
    "Bearing wear": "Review vibration and motor current",
    "Overheating": "Review temperature and motor current",
    "Cavitation": "Review pressure and vibration",
    "Unclassified": "Review all four sensor readings",
}
TRAIN_END = 3000
CALIBRATION_END = 4000
CALIBRATION_CUTOFFS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)


@dataclass(frozen=True)
class PumpModel:
    classifier: RandomForestClassifier
    scaler: StandardScaler
    pca: PCA
    cutoff: float


def simulate_telemetry() -> pd.DataFrame:
    """Reproduce the original notebook's four channels and retain injected labels."""
    rng = np.random.RandomState(42)
    rows = 5000
    timestamps = pd.date_range("2026-11-01", periods=rows, freq="min")
    vibration = rng.normal(2.2, 0.4, rows)
    temperature = rng.normal(62.0, 5.0, rows)
    pressure = rng.normal(4.5, 0.3, rows)
    current = rng.normal(14.0, 1.2, rows)
    injected_fault = np.full(rows, "Nominal", dtype=object)

    for index in rng.choice(rows, 200, replace=False):
        fault = rng.choice(FAULTS)
        injected_fault[index] = fault
        if fault == "Bearing wear":
            vibration[index] += rng.uniform(2.5, 4.5)
            current[index] += rng.uniform(3.0, 6.0)
        elif fault == "Overheating":
            temperature[index] += rng.uniform(22.0, 35.0)
            current[index] += rng.uniform(2.0, 4.0)
        else:
            pressure[index] -= rng.uniform(1.8, 2.8)
            vibration[index] += rng.uniform(1.5, 3.0)

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "machine_id": "PUMP_UNIT_01",
            "vibration_mm_s": np.round(vibration, 3),
            "temperature_c": np.round(temperature, 2),
            "pressure_bar": np.round(pressure, 2),
            "motor_current_amp": np.round(current, 2),
            "injected_fault": injected_fault,
        }
    )


def fit_model(telemetry: pd.DataFrame) -> PumpModel:
    """Train on recorded injection labels and set the cutoff on a later time segment."""
    ordered = telemetry.sort_values("timestamp")
    training = ordered.iloc[:TRAIN_END]
    calibration = ordered.iloc[TRAIN_END:CALIBRATION_END]
    training_labels = training["injected_fault"].ne("Nominal")
    nominal = training.loc[~training_labels, FEATURES]
    scaler = StandardScaler().fit(nominal)
    pca = PCA(n_components=2, svd_solver="full").fit(scaler.transform(nominal))
    classifier = RandomForestClassifier(
        n_estimators=50, max_depth=5, class_weight="balanced", random_state=42, n_jobs=1
    ).fit(training[FEATURES], training_labels)
    calibration_scores = classifier.predict_proba(calibration[FEATURES])[:, 1]
    calibration_labels = calibration["injected_fault"].ne("Nominal")
    cutoff = max(
        CALIBRATION_CUTOFFS,
        key=lambda candidate: (
            f1_score(calibration_labels, calibration_scores >= candidate), candidate
        ),
    )
    return PumpModel(classifier, scaler, pca, cutoff)


def score_readings(readings: pd.DataFrame, model: PumpModel) -> pd.DataFrame:
    """Score incoming readings and attach the review cues used in both reports."""
    scored = readings.copy()
    x = scored[FEATURES]
    scaled = model.scaler.transform(x)
    pca = model.pca
    components = pca.transform(scaled)
    reconstructed = pca.inverse_transform(components)
    scored["pca_1"] = components[:, 0]
    scored["pca_2"] = components[:, 1]
    scored["pca_error"] = np.mean((scaled - reconstructed) ** 2, axis=1)

    scored["fault_score"] = model.classifier.predict_proba(x)[:, 1]
    scored["alert_cutoff"] = model.cutoff
    flagged = scored["fault_score"].ge(model.cutoff)
    scored["anomaly_flag"] = flagged.astype(int)

    bearing = scored["vibration_mm_s"].gt(4.0) & scored["motor_current_amp"].gt(16.0)
    heat = scored["temperature_c"].gt(80.0)
    cavitation = scored["pressure_bar"].lt(3.0)
    scored["suspected_fault"] = np.select(
        [flagged & bearing, flagged & heat, flagged & cavitation, flagged],
        FAULTS + ["Unclassified"],
        default="",
    )
    scored["review_cue"] = scored["suspected_fault"].map(REVIEW_CUES).fillna("")
    return scored


def score_telemetry(telemetry: pd.DataFrame) -> pd.DataFrame:
    """Score the full historical snapshot and label outcomes on the final 1,000 readings."""
    scored = telemetry.sort_values("timestamp").reset_index(drop=True).copy()
    scored["data_split"] = "Holdout"
    scored.loc[: CALIBRATION_END - 1, "data_split"] = "Calibration"
    scored.loc[: TRAIN_END - 1, "data_split"] = "Training"
    scored = score_readings(scored, fit_model(scored))
    nominal = scored["injected_fault"].eq("Nominal")
    flagged = scored["anomaly_flag"].eq(1)
    holdout = scored["data_split"].eq("Holdout")
    scored["outcome"] = np.select(
        [holdout & ~nominal & flagged, holdout & nominal & flagged,
         holdout & ~nominal & ~flagged, holdout & nominal & ~flagged],
        ["True positive", "False positive", "False negative", "True negative"],
        default="",
    )
    return scored


def summarize(scored: pd.DataFrame) -> dict:
    holdout = scored.loc[scored["data_split"].eq("Holdout")]
    outcomes = holdout["outcome"].value_counts()
    tp = int(outcomes.get("True positive", 0))
    fp = int(outcomes.get("False positive", 0))
    fn = int(outcomes.get("False negative", 0))
    tn = int(outcomes.get("True negative", 0))

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    return {
        "readings": len(scored),
        "flagged_minutes": int(scored["anomaly_flag"].sum()),
        "alert_cutoff": float(scored["alert_cutoff"].iloc[0]),
        "injected_fault_minutes": int(scored["injected_fault"].ne("Nominal").sum()),
        "split_readings": {
            segment: int(scored["data_split"].eq(segment).sum())
            for segment in ("Training", "Calibration", "Holdout")
        },
        "holdout": {
            "readings": len(holdout),
            "true_positive": tp,
            "false_positive": fp,
            "false_negative": fn,
            "true_negative": tn,
            "precision": ratio(tp, tp + fp),
            "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn),
            "false_alarm_rate": ratio(fp, fp + tn),
            "by_injected_fault": {
                fault: {
                    "injected": int(holdout["injected_fault"].eq(fault).sum()),
                    "detected": int(holdout.loc[holdout["injected_fault"].eq(fault), "anomaly_flag"].sum()),
                }
                for fault in FAULTS
            },
        },
    }


def _power_query_snapshot(csv_bytes: bytes) -> str:
    # Raw DEFLATE matches Power Query's Compression.Deflate binary format.
    compressor = zlib.compressobj(level=9, wbits=-15)
    compressed = compressor.compress(csv_bytes) + compressor.flush()
    encoded = base64.b64encode(compressed).decode("ascii")
    chunks = ",\n".join(f'\t\t\t"{chunk}"' for chunk in textwrap.wrap(encoded, width=1000))
    return (
        "expression ScoredTelemetryCsv =\n"
        "\t\tlet\n"
        "\t\t\tEncoded = Text.Combine({\n"
        f"{chunks}\n"
        "\t\t\t}),\n"
        "\t\t\tDecoded = Binary.FromText(Encoded, BinaryEncoding.Base64)\n"
        "\t\tin\n"
        "\t\t\tBinary.Decompress(Decoded, Compression.Deflate)\n"
        "\tkind: m\n"
        "\tlineageTag: c48fa570-9b77-49b8-8a19-7eed2acc107d\n"
        "\n"
        "\tannotation PBI_ResultType = Binary\n"
    )


def export_project(scored: pd.DataFrame, metrics: dict) -> None:
    """Write the CSV and the identical self-contained snapshot loaded by Power BI."""
    csv_bytes = scored.to_csv(
        index=False, date_format="%Y-%m-%d %H:%M:%S", lineterminator="\n"
    ).encode("utf-8")
    snapshot = _power_query_snapshot(csv_bytes)
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    MODEL_DEFINITION.mkdir(parents=True, exist_ok=True)
    (data_dir / "pump_telemetry_scored.csv").write_bytes(csv_bytes)
    (data_dir / "model_metrics.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    expressions = MODEL_DEFINITION / "expressions.tmdl"
    if expressions.is_file():
        previous = expressions.read_text(encoding="utf-8")
        before, marker, body = previous.partition("expression ScoredTelemetryCsv =")
        if marker:
            _, next_expression, after = body.partition("\nexpression ")
            snapshot = before + snapshot + next_expression + after
        else:
            snapshot = previous.rstrip() + "\n\n" + snapshot
    expressions.write_text(snapshot, encoding="utf-8")


if __name__ == "__main__":
    readings = score_telemetry(simulate_telemetry())
    summary = summarize(readings)
    export_project(readings, summary)
    print(json.dumps(summary, indent=2))
