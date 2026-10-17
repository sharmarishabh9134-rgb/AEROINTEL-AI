"""Verified analytics for the explicitly synthetic maintenance demo dataset."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from fastapi import HTTPException

DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "aircraft_maintenance_demo.csv"
MODEL_PATH = Path(__file__).resolve().parents[1] / "models" / "maintenance_demo.joblib"
NOTICE = "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE"
TARGET = "demo_maintenance_risk_label"
IDENTIFIERS = {"record_id", "data_origin"}
NUMERIC_FEATURES = [
    "aircraft_age_years", "total_flight_hours", "total_cycles",
    "days_since_last_maintenance", "maintenance_events_last_12_months",
    "unresolved_maintenance_items", "inspection_overdue_flag",
]
CATEGORICAL_FEATURES = ["aircraft_category", "propulsion_type"]
FEATURES = CATEGORICAL_FEATURES + NUMERIC_FEATURES
LABELS = ["Low", "Medium", "High"]
MODEL_VERSION = "synthetic-maintenance-rf-v1"
NUMERIC_RANGES = {
    "aircraft_age_years": (0, 100),
    "total_flight_hours": (0, 200_000),
    "total_cycles": (0, 250_000),
    "days_since_last_maintenance": (0, 3_650),
    "maintenance_events_last_12_months": (0, 100),
    "unresolved_maintenance_items": (0, 100),
}


def dataset_hash(path: Path = DATASET_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset(path: Path = DATASET_PATH) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Maintenance demo dataset is missing: {path.name}")
    frame = pd.read_csv(path)
    required = set(FEATURES + [TARGET, *IDENTIFIERS])
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError("Maintenance dataset is missing required columns: " + ", ".join(missing))
    if frame.empty or frame[TARGET].isna().any() or frame[FEATURES].isna().any().any():
        raise ValueError("Maintenance dataset must contain complete feature and target rows")
    for column, (minimum, maximum) in NUMERIC_RANGES.items():
        try:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Maintenance dataset feature {column} must be numeric") from exc
        if not frame[column].between(minimum, maximum).all():
            raise ValueError(f"Maintenance dataset feature {column} is outside its supported range")
    for column in ("maintenance_events_last_12_months", "unresolved_maintenance_items"):
        if not (frame[column] % 1 == 0).all():
            raise ValueError(f"Maintenance dataset feature {column} must contain whole numbers")
    if not set(frame["inspection_overdue_flag"].unique()).issubset({0, 1, False, True}):
        raise ValueError("Maintenance dataset inspection_overdue_flag must contain only 0 or 1")
    for column in CATEGORICAL_FEATURES:
        if frame[column].astype(str).str.strip().eq("").any():
            raise ValueError(f"Maintenance dataset feature {column} must not contain blank categories")
    if set(frame[TARGET].astype(str)) - set(LABELS):
        raise ValueError("Maintenance dataset contains an unsupported target label")
    if set(frame["data_origin"].astype(str)) != {"SYNTHETIC_DEMO"}:
        raise ValueError("Maintenance dataset must be explicitly marked SYNTHETIC_DEMO")
    return frame


def dataset_summary(path: Path = DATASET_PATH) -> dict[str, Any]:
    frame = load_dataset(path)
    counts = frame[TARGET].value_counts().to_dict()
    return {
        "rows": int(len(frame)),
        "feature_count": len(FEATURES),
        "class_distribution": {label: int(counts.get(label, 0)) for label in LABELS},
        "aircraft_categories": sorted(frame["aircraft_category"].astype(str).unique().tolist()),
        "propulsion_types": sorted(frame["propulsion_type"].astype(str).unique().tolist()),
        "dataset_sha256": dataset_hash(path),
        "data_origin": "SYNTHETIC_DEMO",
        "notice": NOTICE,
    }


def train_model(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    """Explicit training entry point; API handlers never call this function."""
    from sklearn.compose import ColumnTransformer
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder

    frame = load_dataset(dataset_path)
    x_train, x_test, y_train, y_test = train_test_split(
        frame[FEATURES], frame[TARGET].astype(str), test_size=0.2,
        random_state=42, stratify=frame[TARGET].astype(str),
    )
    preprocessing = ColumnTransformer([
        ("numeric", SimpleImputer(strategy="median"), NUMERIC_FEATURES),
        ("categorical", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")),
                                  ("onehot", OneHotEncoder(handle_unknown="ignore"))]), CATEGORICAL_FEATURES),
    ])
    pipeline = Pipeline([
        ("preprocessing", preprocessing),
        ("classifier", RandomForestClassifier(n_estimators=300, class_weight="balanced",
                                               random_state=42, min_samples_leaf=2)),
    ])
    pipeline.fit(x_train, y_train)
    predicted = pipeline.predict(x_test)
    report = classification_report(y_test, predicted, labels=LABELS, output_dict=True, zero_division=0)
    metrics = {
        "accuracy": float(accuracy_score(y_test, predicted)),
        "macro_avg": {key: float(report["macro avg"][key]) for key in ("precision", "recall", "f1-score")},
        "weighted_avg": {key: float(report["weighted avg"][key]) for key in ("precision", "recall", "f1-score")},
        "per_class": {label: {key: float(report[label][key]) for key in ("precision", "recall", "f1-score", "support")}
                      for label in LABELS},
        "confusion_matrix": confusion_matrix(y_test, predicted, labels=LABELS).tolist(),
        "labels": LABELS,
        "test_rows": int(len(y_test)),
    }
    artifact = {
        "model": pipeline,
        "model_version": MODEL_VERSION,
        "feature_schema": FEATURES,
        "metrics": metrics,
        "dataset_sha256": dataset_hash(dataset_path),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": int(len(x_train)),
        "test_rows": int(len(x_test)),
        "random_state": 42,
        "notice": NOTICE,
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path)
    return {key: value for key, value in artifact.items() if key != "model"}


def _metrics_are_valid(metrics: Any) -> bool:
    if not isinstance(metrics, dict) or not isinstance(metrics.get("labels"), list):
        return False
    labels = metrics["labels"]
    if labels != LABELS or not isinstance(metrics.get("test_rows"), int) or metrics["test_rows"] <= 0:
        return False
    if not isinstance(metrics.get("confusion_matrix"), list) or len(metrics["confusion_matrix"]) != len(LABELS):
        return False
    matrix = metrics["confusion_matrix"]
    if any(not isinstance(row, list) or len(row) != len(LABELS)
           or any(not isinstance(value, int) or value < 0 for value in row) for row in matrix):
        return False
    if sum(sum(row) for row in matrix) != metrics["test_rows"]:
        return False
    if not isinstance(metrics.get("per_class"), dict) or not isinstance(metrics.get("macro_avg"), dict):
        return False
    if not isinstance(metrics.get("weighted_avg"), dict):
        return False
    values = [metrics.get("accuracy")]
    support_total = 0
    for group in (metrics["macro_avg"], metrics["weighted_avg"]):
        values.extend(group.get(key) for key in ("precision", "recall", "f1-score"))
    for label in LABELS:
        item = metrics["per_class"].get(label)
        if not isinstance(item, dict):
            return False
        values.extend(item.get(key) for key in ("precision", "recall", "f1-score"))
        support = item.get("support")
        if not isinstance(support, (int, float)) or not math.isfinite(support) or support < 0:
            return False
        support_total += support
    return (all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in values)
            and support_total == metrics["test_rows"])


def _artifact_state(dataset_path: Path, model_path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    status: dict[str, Any] = {"available": False, "status": "not_trained", "notice": NOTICE,
                              "model_version": MODEL_VERSION}
    try:
        frame = load_dataset(dataset_path)
        current_hash = dataset_hash(dataset_path)
    except OSError:
        return {**status, "status": "dataset_unavailable"}, None
    except ValueError:
        return {**status, "status": "dataset_invalid"}, None
    if not model_path.is_file():
        return {**status, "dataset_sha256": current_hash}, None
    try:
        artifact = joblib.load(model_path)
        if artifact.get("dataset_sha256") != current_hash:
            return {**status, "status": "stale", "dataset_sha256": current_hash}, None
        model = artifact.get("model")
        feature_names = list(getattr(model, "feature_names_in_", []))
        stored_schema = artifact.get("feature_schema", feature_names)
        classifier = getattr(model, "named_steps", {}).get("classifier")
        model_classes = list(getattr(classifier, "classes_", []))
        training_rows, test_rows = int(artifact["training_rows"]), int(artifact["test_rows"])
        if (not callable(getattr(model, "predict", None)) or feature_names != FEATURES
                or stored_schema != FEATURES or set(model_classes) != set(LABELS) or not artifact.get("trained_at")
                or training_rows <= 0 or test_rows <= 0 or training_rows + test_rows != len(frame)
                or not _metrics_are_valid(artifact.get("metrics"))
                or artifact["metrics"].get("test_rows") != test_rows):
            return {**status, "status": "incompatible_artifact", "dataset_sha256": current_hash}, None
        details = {"available": True, "status": "ready", "model_version": artifact.get("model_version", MODEL_VERSION),
                   "trained_at": artifact["trained_at"], "training_rows": training_rows,
                   "test_rows": test_rows, "dataset_sha256": current_hash, "notice": NOTICE}
        return details, artifact
    except Exception:
        return {**status, "status": "invalid_artifact", "dataset_sha256": current_hash}, None


def model_status(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    return _artifact_state(dataset_path, model_path)[0]


def _load_ready_artifact(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    status, artifact = _artifact_state(dataset_path, model_path)
    if artifact is None:
        raise HTTPException(status_code=503, detail=f"ML model is {status['status']}; run the explicit model training command.")
    return artifact


def evaluation_metrics(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    artifact = _load_ready_artifact(dataset_path, model_path)
    return {**artifact["metrics"], "trained_at": artifact["trained_at"], "notice": NOTICE,
            "evaluation_basis": "Fixed stratified 20% holdout split; random_state=42."}


def predict(values: dict[str, Any], dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    artifact = _load_ready_artifact(dataset_path, model_path)
    frame_data = load_dataset(dataset_path)
    for feature, source in (("aircraft_category", "aircraft_category"), ("propulsion_type", "propulsion_type")):
        supported = set(frame_data[source].astype(str).unique())
        if values[feature] not in supported:
            choices = ", ".join(sorted(supported))
            raise HTTPException(status_code=422, detail=f"Unsupported {feature.replace('_', ' ')}. Choose one of: {choices}.")
    frame = pd.DataFrame([{key: values[key] for key in FEATURES}], columns=FEATURES)
    label = str(artifact["model"].predict(frame)[0])
    return {"predicted_demo_label": label, "model_version": artifact.get("model_version", MODEL_VERSION),
            "model_status": "ready", "notice": NOTICE,
            "explanation": "This is a synthetic demonstration model output, not an assessment of an actual aircraft."}


def assistant_results(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    summary = dataset_summary(dataset_path)
    status = model_status(dataset_path, model_path)
    result: dict[str, Any] = {"status": status, "dataset": summary, "notice": NOTICE}
    if status["available"]:
        result["evaluation"] = evaluation_metrics(dataset_path, model_path)
    return result
