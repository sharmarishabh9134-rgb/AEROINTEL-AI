from pathlib import Path
import pandas as pd
import pytest

from backend.app.fleet_intelligence import (
    generate_synthetic_data, load_cmapss, map_snag, preprocess, rag_answer,
)


def test_synthetic_generator_is_repeatable_and_complete():
    a = generate_synthetic_data(seed=9)
    b = generate_synthetic_data(seed=9)
    assert a == b
    assert all(a[key] for key in ("tech_logs", "work_orders", "spares", "depots"))
    assert len(a["tech_logs"]) == 24


def test_noisy_snag_maps_to_expected_component():
    result = map_snag("crew says oil pressure fluctuates on climb pls chk")
    assert result["component"] == "oil pressure sensor"
    assert 0 < result["similarity"] <= 1


def test_cmapss_loader_parses_local_fd001(tmp_path):
    row = "1 1 0.1 0.2 100 " + " ".join(str(200+i) for i in range(21))
    (tmp_path / "train_FD001.txt").write_text(row + "\n", encoding="utf-8")
    frame = load_cmapss("train", "FD001", tmp_path)
    assert frame.shape == (1, 26)
    assert frame.iloc[0]["sensor_01"] == 200


def test_cmapss_loader_fails_explicitly_when_dataset_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="Place the NASA C-MAPSS"):
        load_cmapss("train", "FD001", tmp_path)


def test_smoothing_and_change_point_columns():
    frame = pd.DataFrame({"unit": [1]*5, "cycle": range(1, 6), "sensor_01": [1, 1, 1, 1, 30]})
    out = preprocess(frame, window=3)
    assert out.sensor_01.iloc[-1] < 30
    assert "change_point" in out


def test_rag_returns_sources_and_cases():
    result = rag_answer("oil pressure fluctuates during climb")
    assert result["citations"]
    assert any(x["id"].startswith("CASE-") for x in result["similar_cases"])


def test_aircraft_graph_route_is_registered():
    from backend.app.main import app
    paths = {route.path for route in app.routes}
    assert "/graph/aircraft/{aircraft_id}" in paths


def test_fd001_not_trained_response_includes_training_instructions(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import backend.app.fleet_intelligence as intelligence
    monkeypatch.setattr(intelligence, "MODEL_PATH", tmp_path / "missing.joblib")
    with pytest.raises(HTTPException) as exc:
        intelligence.fd001_metrics(None)
    assert exc.value.status_code == 503
    assert "python -m backend.scripts.train_fd001" in exc.value.detail

