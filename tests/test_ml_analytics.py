from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException
from backend.app.schemas import MaintenanceDemoPrediction

from backend.app import ml_analytics as ml


@pytest.fixture
def local_tmp():
    with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
        yield Path(directory)


def test_supplied_dataset_is_validated_and_excludes_identifiers_from_features():
    frame = ml.load_dataset()
    summary = ml.dataset_summary()
    assert len(frame) == 150
    assert summary["class_distribution"] == {"Low": 56, "Medium": 75, "High": 19}
    assert summary["data_origin"] == "SYNTHETIC_DEMO"
    assert "record_id" not in ml.FEATURES
    assert "data_origin" not in ml.FEATURES
    assert ml.TARGET not in ml.FEATURES
    assert list(MaintenanceDemoPrediction.model_fields) == ml.FEATURES


def test_training_reports_actual_holdout_metrics_and_stale_artifact(local_tmp):
    dataset_copy = local_tmp / "dataset.csv"
    shutil.copyfile(ml.DATASET_PATH, dataset_copy)
    model_path = local_tmp / "model.joblib"
    result = ml.train_model(dataset_copy, model_path)
    assert result["training_rows"] == 120
    assert result["test_rows"] == 30
    metrics = result["metrics"]
    assert result["feature_schema"] == ml.FEATURES
    assert result["model_version"] == ml.MODEL_VERSION
    assert 0 <= metrics["accuracy"] <= 1
    assert sum(sum(row) for row in metrics["confusion_matrix"]) == 30
    assert set(metrics["per_class"]) == set(ml.LABELS)
    assert ml.model_status(dataset_copy, model_path)["available"] is True
    dataset_copy.write_text(dataset_copy.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert ml.model_status(dataset_copy, model_path)["status"] == "stale"


def test_prediction_uses_trained_artifact_and_marks_synthetic_output(local_tmp):
    model_path = local_tmp / "model.joblib"
    ml.train_model(model_path=model_path)
    values = {
        "aircraft_category": "Transport", "propulsion_type": "Jet",
        "aircraft_age_years": 12, "total_flight_hours": 4200, "total_cycles": 2300,
        "days_since_last_maintenance": 45, "maintenance_events_last_12_months": 2,
        "unresolved_maintenance_items": 0, "inspection_overdue_flag": False,
    }
    result = ml.predict(values, model_path=model_path)
    assert result["predicted_demo_label"] in ml.LABELS
    assert result["notice"] == ml.NOTICE
    assert "not an assessment of an actual aircraft" in result["explanation"]
    assert result["model_status"] == "ready"
    assert result["model_version"] == ml.MODEL_VERSION


def test_prediction_rejects_categories_not_supported_by_training_data(local_tmp):
    model_path = local_tmp / "model.joblib"
    ml.train_model(model_path=model_path)
    values = {
        "aircraft_category": "Unknown aircraft", "propulsion_type": "Jet",
        "aircraft_age_years": 12, "total_flight_hours": 4200, "total_cycles": 2300,
        "days_since_last_maintenance": 45, "maintenance_events_last_12_months": 2,
        "unresolved_maintenance_items": 0, "inspection_overdue_flag": False,
    }
    with pytest.raises(HTTPException) as error:
        ml.predict(values, model_path=model_path)
    assert error.value.status_code == 422
    assert "Unsupported aircraft category" in error.value.detail


def test_incompatible_artifact_is_not_reported_ready(local_tmp):
    import joblib

    model_path = local_tmp / "model.joblib"
    ml.train_model(model_path=model_path)
    artifact = joblib.load(model_path)
    artifact["feature_schema"] = ["wrong_feature"]
    joblib.dump(artifact, model_path)
    assert ml.model_status(model_path=model_path)["status"] == "incompatible_artifact"


def test_missing_model_is_not_trained_implicitly(local_tmp):
    status = ml.model_status(model_path=local_tmp / "missing.joblib")
    assert status["available"] is False
    assert status["status"] == "not_trained"
