"""Portable inference for the notebook's tuned decision tree."""

import hashlib
import json
import math
import struct
from pathlib import Path


FEATURES = {
    "air_temperature_k": ("Air temperature [K]", 200, 400),
    "process_temperature_k": ("Process temperature [K]", 200, 400),
    "rotational_speed_rpm": ("Rotational speed [rpm]", 500, 5000),
    "torque_nm": ("Torque [Nm]", 0, 150),
    "tool_wear_min": ("Tool wear [min]", 0, 500),
}
INPUT_FIELDS = {"grade", *FEATURES}
FEATURE_ORDER = [*FEATURES, "grade_l", "grade_m"]
REVIEW_THRESHOLD = 0.5
FLOAT32 = struct.Struct("=f")


def model_version(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "ai4i-dt-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def validate_features(data):
    grade = data.get("grade")
    if grade not in ("H", "L", "M"):
        raise ValueError("grade must be H, L, or M.")
    result = {"grade": grade}
    for key, (_, minimum, maximum) in FEATURES.items():
        value = data.get(key)
        if type(value) not in (int, float) or not minimum <= value <= maximum:
            raise ValueError(f"{key} must be a finite number from {minimum} to {maximum}.")
        result[key] = float(value)
    return result


class FailureModel:
    def __init__(self, path):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.version = payload.pop("model_version")
        if self.version != model_version(payload) or payload["features"] != FEATURE_ORDER:
            raise ValueError("Model version or feature contract does not match its contents.")
        self.mean = payload["mean"]
        self.scale = payload["scale"]
        self.nodes = payload["nodes"]
        if len(self.mean) != len(FEATURE_ORDER) or len(self.scale) != len(FEATURE_ORDER):
            raise ValueError("Model preprocessing has the wrong number of features.")
        if not self.nodes or any(not math.isfinite(value) or value <= 0 for value in self.scale):
            raise ValueError("Model preprocessing or decision nodes are invalid.")

    def score(self, features):
        values = [features[key] for key in FEATURES]
        values.extend([features["grade"] == "L", features["grade"] == "M"])
        # scikit-learn casts tree inputs to float32 after the scaler runs.
        scaled = [
            FLOAT32.unpack(FLOAT32.pack((value - mean) / scale))[0]
            for value, mean, scale in zip(values, self.mean, self.scale)
        ]
        node = self.nodes[0]
        while "feature" in node:
            value = scaled[node["feature"]]
            node = self.nodes[node["left"] if value <= node["threshold"] else node["right"]]
        return node["score"]

    def predict(self, features):
        score = self.score(features)
        review = int(score > REVIEW_THRESHOLD)
        return {
            "model_score": score,
            "review_flag": review,
            "status": "Review" if review else "Normal score",
            "review_threshold": REVIEW_THRESHOLD,
            "model_version": self.version,
        }
