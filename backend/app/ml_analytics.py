"""Verified analytics for the explicitly synthetic maintenance demo dataset."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
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


def model_status(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    status: dict[str, Any] = {"available": False, "status": "not_trained", "notice": NOTICE}
    try:
        current_hash = dataset_hash(dataset_path)
    except OSError:
        return {**status, "status": "dataset_unavailable"}
    if not model_path.is_file():
        return {**status, "dataset_sha256": current_hash}
    try:
        artifact = joblib.load(model_path)
        if artifact.get("dataset_sha256") != current_hash:
            return {**status, "status": "stale", "dataset_sha256": current_hash}
        return {"available": True, "status": "ready", "trained_at": artifact["trained_at"],
                "training_rows": artifact["training_rows"], "test_rows": artifact["test_rows"],
                "dataset_sha256": current_hash, "notice": NOTICE}
    except Exception:
        return {**status, "status": "invalid_artifact", "dataset_sha256": current_hash}


def _load_ready_artifact(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    status = model_status(dataset_path, model_path)
    if not status["available"]:
        raise HTTPException(status_code=503, detail=f"ML model is {status['status']}; run the explicit model training command.")
    return joblib.load(model_path)


def evaluation_metrics(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    artifact = _load_ready_artifact(dataset_path, model_path)
    return {**artifact["metrics"], "trained_at": artifact["trained_at"], "notice": NOTICE,
            "evaluation_basis": "Fixed stratified 20% holdout split; random_state=42."}


def predict(values: dict[str, Any], dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    artifact = _load_ready_artifact(dataset_path, model_path)
    frame = pd.DataFrame([{key: values[key] for key in FEATURES}], columns=FEATURES)
    label = str(artifact["model"].predict(frame)[0])
    return {"predicted_demo_label": label, "notice": NOTICE,
            "explanation": "This is a synthetic demonstration model output, not an assessment of an actual aircraft."}


def assistant_results(dataset_path: Path = DATASET_PATH, model_path: Path = MODEL_PATH) -> dict[str, Any]:
    summary = dataset_summary(dataset_path)
    status = model_status(dataset_path, model_path)
    result: dict[str, Any] = {"status": status, "dataset": summary, "notice": NOTICE}
    if status["available"]:
        result["evaluation"] = evaluation_metrics(dataset_path, model_path)
    return result
