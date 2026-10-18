import os
from pathlib import Path
import tempfile
import uuid
import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Set backend config before importing the app so this suite never touches a developer DB.
TEST_DB = Path(tempfile.gettempdir()) / f"aerointel_test_{uuid.uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["DEMO_MODE"] = "true"
os.environ["AUTO_SEED_DEMO_DATA"] = "true"
os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET_KEY"] = "test-only-secret-with-at-least-32-characters"

from backend.app.config import get_settings
get_settings.cache_clear()

# Pytest plugins or earlier imports may have initialized the application engine
# before this module's isolated test DATABASE_URL was set. Rebind it explicitly.
import backend.app.database as database
database.engine.dispose()
database.engine = database.make_engine(os.environ["DATABASE_URL"])
database.SessionLocal.configure(bind=database.engine)

from fastapi.testclient import TestClient
from backend.app.main import app
import backend.app.main as main_module
main_module.engine = database.engine
main_module.settings = get_settings()
from backend.app.database import engine
from backend.app.database import SessionLocal
from backend.app.models import Profile
from sqlalchemy import select


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        login = test_client.post("/api/auth/register", json={
            "email": "integration-reader@example.com",
            "password": "integration-reader-passphrase",
        })
        assert login.status_code == 201
        test_client.headers["Authorization"] = f"Bearer {login.json()['data']['access_token']}"
        yield test_client
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture(autouse=True)
def reset_in_process_rate_limits_between_tests():
    # Keep integration tests independent; production throttling is unchanged.
    main_module._attempts.clear()


def test_health_and_seeded_dashboard(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    health_data = health.json()["data"]
    assert health_data["database"] == "connected"
    assert health_data["database_dialect"] == "sqlite"
    assert health_data["data_source_mode"] == "synthetic_demo"
    assert health_data["migration_status"]["history_tracking"] == "not_configured"
    assert health_data["migration_status"]["schema_status"] == "compatible"
    assert health_data["table_counts_status"] == "complete"
    assert health_data["table_counts"]["aircraft"] == 48
    assert health_data["table_counts"]["crew"] == 25
    assert "profiles" not in health_data["table_counts"]
    assert not {"database_url", "jwt_secret_key", "password", "api_key"}.intersection(health_data)
    summary = client.get("/api/dashboard/summary").json()["data"]
    assert summary["aircraft_total"] == 48
    assert summary["public_inventory_estimate"]["reported_total"] == 2350
    assert summary["public_inventory_estimate"]["listed_row_total"] == 2353
    assert summary["aircraft_available"] > 0
    assert summary["meta"]["data_mode"] == "demo"
    assert summary["readiness_pct"] == 88  # 42 explicit confirmations / 48 explicit booleans
    assert summary["availability_pct"] == 71  # 34 available / 48 recognized statuses
    assert summary["resource_utilization_pct"] is None
    assert summary["metric_basis"]["rounding"] == "nearest whole percentage; exact half values round up"
    assert summary["maintenance_open"] == 9


def test_dashboard_page_endpoints_have_seeded_rows_and_period_filters(client):
    import backend.app.database as database
    import backend.app.dashboard_demo as demo_module
    from backend.app.models import Base
    temp_engine = database.make_engine(f"sqlite:///{(Path(tempfile.gettempdir()) / f'aerointel_dashboard_{uuid.uuid4().hex}.db').as_posix()}")
    old_engine = database.engine
    old_main_engine = main_module.engine
    old_demo_engine = demo_module.engine
    Base.metadata.create_all(bind=temp_engine)
    database.engine = temp_engine
    database.SessionLocal.configure(bind=temp_engine)
    main_module.engine = temp_engine
    demo_module.engine = temp_engine
    try:
        registration = client.post("/api/auth/register", json={"email":"dashboard-data@example.com", "password":"dashboard-data-test-passphrase"})
        assert registration.status_code == 201
        token = registration.json()["data"]["access_token"]
        headers = {"Authorization":f"Bearer {token}"}
        with database.SessionLocal() as db:
            counts = demo_module.seed_dashboard_demo_data(db)
        assert all(counts[key] > 0 for key in ("aircraft", "crew", "maintenance", "schedules", "alerts", "reports"))
        recalculated = client.post("/api/reports/recalculate?days=7", headers=headers)
        assert recalculated.status_code == 200
        assert recalculated.json()["data"]["report_type"] == "operational_snapshot"
        for route in ("/api/aircraft", "/api/crew", "/api/maintenance", "/api/schedules", "/api/alerts", "/api/reports"):
            response = client.get(route, headers=headers)
            assert response.status_code == 200, route
            assert isinstance(response.json().get("data"), list), route
            assert response.json()["data"], route
            assert any(row.get("is_demo") is True for row in response.json()["data"]), route
        one_day = client.get("/api/schedules?days=1", headers=headers).json()["data"]
        month = client.get("/api/schedules?days=30", headers=headers).json()["data"]
        assert len(one_day) < len(month)
    finally:
        database.SessionLocal.configure(bind=old_engine)
        database.engine = old_engine
        main_module.engine = old_main_engine
        demo_module.engine = old_demo_engine
        temp_engine.dispose()
        Path(temp_engine.url.database).unlink(missing_ok=True)


def test_current_aircraft_and_crew_rosters_keep_older_records_visible(client):
    from backend.app.models import Aircraft, Crew

    cutoff_old = datetime.now(timezone.utc) - timedelta(days=120)
    aircraft_id = f"OLD-A-{uuid.uuid4().hex[:8]}"
    crew_id = f"OLD-C-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as db:
        db.add(Aircraft(resource_id=aircraft_id, category="Transport", availability_status="available",
                        maintenance_status="clear", updated_at=cutoff_old))
        db.add(Crew(display_id=crew_id, group_name="Current roster", readiness_status="ready",
                    confirmation_at=cutoff_old))
        db.commit()

    assert aircraft_id in {row["resource_id"] for row in client.get("/api/aircraft?days=1").json()["data"]}
    assert crew_id in {row["display_id"] for row in client.get("/api/crew?days=1").json()["data"]}


def test_public_aviation_provider_failure_uses_labelled_fallback(client, monkeypatch):
    import backend.app.main as main
    from backend.app.models import ScheduleRecord

    with SessionLocal() as db:
        db.add(ScheduleRecord(title="Synthetic fallback flight", category="Flight · synthetic demo",
                              status="scheduled", starts_at=datetime.now(timezone.utc), is_demo=True))
        db.commit()

    async def offline(_airports):
        return {"data": {"metars": [], "metar_source": "unavailable", "opensky_source": "unavailable",
                         "opensky_aircraft_count": None}, "notices": ["provider offline"]}

    monkeypatch.setattr(main, "fetch_public_aviation_data", offline)
    response = client.get("/api/public-aviation-data")
    assert response.status_code == 200
    fallback = response.json()["data"]["fallback"]
    assert fallback["is_demo"] is True
    assert fallback["data_origin"] == "SYNTHETIC_DEMO_FALLBACK"
    assert fallback["weather_observations"]
    assert fallback["scheduled_demo_flights"] > 0


def test_sqlite_reinitialization_preserves_existing_records(client):
    from backend.app.models import (Aircraft, Alert, AuditLog, Crew, MaintenanceRecord,
                            PublicInventoryRecord, ScheduleRecord, WeatherObservation)

    models = (Aircraft, Crew, MaintenanceRecord, ScheduleRecord, Alert, AuditLog,
              WeatherObservation, PublicInventoryRecord)
    with SessionLocal() as db:
        before = {model.__tablename__: db.query(model).count() for model in models}
    main_module.initialize_database()
    with SessionLocal() as db:
        after = {model.__tablename__: db.query(model).count() for model in models}
    assert after == before


def test_frontend_runtime_config_and_dev_cors(client):
    config = client.get("/api/client-config.js")
    assert config.status_code == 200
    assert "AEROINTEL_CONFIG" in config.text
    response = client.options("/api/dashboard/summary", headers={"Origin":"http://localhost:5500", "Access-Control-Request-Method":"GET"})
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5500"
    delete_preflight = client.options("/api/aircraft/1", headers={"Origin":"http://localhost:5500", "Access-Control-Request-Method":"DELETE", "Access-Control-Request-Headers":"authorization"})
    assert delete_preflight.status_code == 200
    assert "DELETE" in delete_preflight.headers.get("access-control-allow-methods", "")


def test_production_cors_excludes_development_origins():
    from types import SimpleNamespace
    import backend.app.main as main
    development_origins = main.cors_origins_for(SimpleNamespace(frontend_url="https://dashboard.example.com"), True)
    assert development_origins == {"https://dashboard.example.com"}
    assert main.cors_origins_for(SimpleNamespace(frontend_url="http://127.0.0.1:8000"), True) == set()


def test_dashboard_reads_are_public_while_writes_remain_protected(client):
    token = client.headers.pop("Authorization")
    try:
        health = client.get("/api/health")
        assert health.status_code == 200
        for route in ("/api/dashboard/summary", "/api/aircraft", "/api/crew", "/api/maintenance", "/api/schedules", "/api/alerts", "/api/activity", "/api/analytics/trends", "/api/weather/current", "/api/public-inventory", "/api/ml/status", "/api/reports"):
            assert client.get(route).status_code == 200, route
        assert client.post("/api/alerts", json={"severity":"low", "category":"Demo", "message":"Guest write"}).status_code == 401
        assert client.post("/api/imports/preview", data={"dataset_type": "aircraft"}, files={"file": ("x.csv", b"resource_id,category\nA-1,Transport\n", "text/csv")}).status_code == 401
        assert client.get("/api/imports/history").status_code == 401
    finally:
        client.headers["Authorization"] = token


def test_schedule_index_metadata_matches_initial_migration():
    from backend.app.models import ScheduleRecord

    index = next(item for item in ScheduleRecord.__table__.indexes if {column.name for column in item.columns} == {"starts_at", "status"})
    migration = (Path(__file__).parents[1] / "backend" / "migrations" / "001_initial_schema.sql").read_text(encoding="utf-8")
    assert index.name == "ix_schedule_starts_status"
    assert {column.name for column in index.columns} == {"starts_at", "status"}
    assert "create index if not exists ix_schedule_starts_status on schedule_records(starts_at,status);" in migration.lower()


def test_demo_seeding_requires_nonproduction_opt_in_and_is_idempotent(monkeypatch):
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    import backend.app.main as main
    from backend.app.models import Aircraft, Crew

    isolated_engine = create_engine("sqlite://")
    monkeypatch.setattr(main, "engine", isolated_engine)
    monkeypatch.setattr(main.settings, "app_env", "test")
    monkeypatch.setattr(main.settings, "demo_mode", True)
    monkeypatch.setattr(main.settings, "auto_seed_demo_data", False)

    main.initialize_database()
    with Session(isolated_engine) as db:
        assert db.scalar(select(func.count()).select_from(Aircraft)) == 0
        assert db.scalar(select(func.count()).select_from(Crew)) == 0

    monkeypatch.setattr(main.settings, "auto_seed_demo_data", True)
    main.initialize_database()
    main.initialize_database()
    with Session(isolated_engine) as db:
        aircraft = db.scalars(select(Aircraft)).all()
        crew = db.scalars(select(Crew)).all()
        assert len(aircraft) == 48
        assert len(crew) == 25
        assert all(row.is_demo and row.data_origin == "synthetic_demo" for row in aircraft + crew)
    isolated_engine.dispose()


def test_production_rejects_sqlite_database(client, monkeypatch):
    import backend.app.main as main
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "demo_mode", False)
    monkeypatch.setattr(main.settings, "jwt_secret_key", "production-test-secret-with-at-least-thirty-two-characters")
    with pytest.raises(RuntimeError, match="must point to PostgreSQL"):
        main.initialize_database()


def test_production_requires_jwt_secret(client, monkeypatch):
    import backend.app.main as main
    monkeypatch.setattr(main.settings, "app_env", "production")
    monkeypatch.setattr(main.settings, "jwt_secret_key", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY must be set"):
        main.initialize_database()


def test_registration_login_and_server_assigned_viewer_role(client):
    registered = client.post("/api/auth/register", json={"email":"sample@example.com", "password":"a-strong-demo-passphrase"})
    assert registered.status_code == 201
    assert registered.json()["data"]["user"]["role"] == "viewer"
    token = registered.json()["data"]["access_token"]
    assert client.get("/api/auth/me", headers={"Authorization":f"Bearer {token}"}).json()["data"]["email"] == "sample@example.com"
    login = client.post("/api/auth/login", json={"email":"sample@example.com", "password":"a-strong-demo-passphrase"})
    assert login.status_code == 200
    assert client.post("/api/auth/login", json={"email":"sample@example.com", "password":"incorrect"}).status_code == 401


def test_signed_in_viewers_can_add_records_without_edit_privileges(client):
    token = client.headers.pop("Authorization")
    assert client.post("/api/alerts", json={"severity":"low", "category":"Demo", "message":"Guest write"}).status_code == 401
    assert client.get("/api/reports").status_code == 200
    client.headers["Authorization"] = token
    registration = client.post("/api/auth/register", json={"email":"viewer@example.com", "password":"another-strong-passphrase"}).json()
    token = registration["data"]["access_token"]
    headers = {"Authorization":f"Bearer {token}"}
    assert registration["data"]["user"]["role"] == "viewer"
    alert = client.post("/api/alerts", headers=headers, json={"severity":"low", "category":"Demo", "message":"A viewer-submitted alert"})
    assert alert.status_code == 201
    aircraft = client.post("/api/aircraft", headers=headers, json={"resource_id":f"VIEW-{uuid.uuid4().hex[:8].upper()}", "category":"Transport", "availability_status":"available", "maintenance_status":"clear"})
    assert aircraft.status_code == 201
    crew = client.post("/api/crew", headers=headers, json={"display_id":f"VIEW-{uuid.uuid4().hex[:8].upper()}", "group_name":"Viewer entry", "readiness_status":"ready"})
    assert crew.status_code == 201
    maintenance = client.post("/api/maintenance", headers=headers, json={"title":f"Viewer maintenance {uuid.uuid4().hex[:6]}", "status":"open"})
    assert maintenance.status_code == 201
    starts_at = (datetime.now(timezone.utc) + timedelta(days=40)).replace(microsecond=0)
    ends_at = starts_at + timedelta(hours=1)
    schedule = client.post("/api/schedules", headers=headers, json={"title":f"Viewer schedule {uuid.uuid4().hex[:6]}", "category":"training", "status":"scheduled", "starts_at":starts_at.isoformat(), "ends_at":ends_at.isoformat()})
    assert schedule.status_code == 201
    assert client.patch(f"/api/alerts/{alert.json()['data']['id']}/acknowledge", headers=headers).status_code == 403


def test_ai_rule_fallback_for_authenticated_viewer(client, monkeypatch):
    import backend.app.services as services
    from backend.app.config import get_settings
    monkeypatch.setattr(services, "get_settings", lambda: get_settings().model_copy(update={"gemini_api_key":None}))
    registration = client.post("/api/auth/register", json={"email":"analysis@example.com", "password":"analysis-demo-passphrase"}).json()
    token = registration["data"]["access_token"]
    response = client.post("/api/ai/analyze", headers={"Authorization":f"Bearer {token}"}, json={"context":{"readiness_pct":1,"alerts_open":999},"question":"Summarize the dashboard snapshot"})
    assert response.status_code == 200
    assert response.json()["data"]["source"] == "rules-fallback"
    assert response.json()["data"]["source_label"] == "Rule-based fallback"
    assert response.json()["data"]["provider"] == {"name":"Gemini","configured":False,"used":False}
    assert response.json()["data"]["verified"] is False
    snapshot = client.get("/api/dashboard/summary").json()["data"]
    readiness_fact = next(f["value"] for f in response.json()["data"]["calculated_data"] if f["label"] == "Aircraft readiness confirmation")
    assert f"{snapshot['aircraft_ready_confirmed']}/{snapshot['readiness_confirmation_records']} ({snapshot['readiness_pct']}%)" in readiness_fact
    assert "999" not in response.text


@pytest.mark.parametrize("question", ["hey", "heyy", "hello", "thanks"])
def test_ai_greetings_and_thanks_skip_provider_and_dashboard_data(monkeypatch, question):
    import backend.app.services as services
    from backend.app.config import get_settings
    from google import genai

    configured = get_settings().model_copy(update={"gemini_api_key":"configured-but-not-used"})
    monkeypatch.setattr(services, "get_settings", lambda: configured)
    monkeypatch.setattr(genai, "Client", lambda **kwargs: (_ for _ in ()).throw(AssertionError("intent handler called Gemini")))
    result = asyncio.run(services.analyze({"readiness_pct":99,"alerts_open":88}, question))
    expected_intent = "thanks" if question == "thanks" else "greeting"
    assert result["intent"] == expected_intent
    assert result["source"] == "intent-handler"
    assert result["source_label"] == "Intent handler"
    assert result["provider"] == {"name":"Gemini","configured":True,"used":False}
    assert result["calculated_data"] == []
    assert result["recommendations"] == []
    assert "99" not in result["summary"] and "88" not in result["summary"]
    assert "confidence" not in result


def test_ai_empty_and_unrelated_messages_do_not_trigger_dashboard_analysis(monkeypatch):
    import backend.app.services as services
    from backend.app.config import get_settings

    settings = get_settings().model_copy(update={"gemini_api_key":None})
    monkeypatch.setattr(services, "get_settings", lambda: settings)
    empty = asyncio.run(services.analyze({"readiness_pct":99}, "  "))
    unrelated = asyncio.run(services.analyze({"readiness_pct":99}, "What is the capital of France?"))
    assert empty["intent"] == "empty" and "Enter a question" in empty["summary"]
    assert unrelated["intent"] == "unrelated" and "dashboard" in unrelated["summary"]
    for result in (empty, unrelated):
        assert result["source_label"] == "Intent handler"
        assert result["calculated_data"] == []
        assert result["provider"]["used"] is False
        assert "confidence" not in result


def test_ai_non_analysis_api_messages_do_not_build_dashboard_snapshot(client, monkeypatch):
    import backend.app.main as main
    user = client.post("/api/auth/register", json={"email":"greeting@example.com", "password":"greeting-test-passphrase"}).json()["data"]
    headers = {"Authorization":f"Bearer {user['access_token']}"}

    def unexpected_snapshot(_db):
        raise AssertionError("Greeting should not calculate a dashboard snapshot")

    monkeypatch.setattr(main, "dashboard_summary", unexpected_snapshot)
    for question, expected in (("heyy", "greeting"), ("", "empty"), ("What is the capital of France?", "unrelated")):
        response = client.post("/api/ai/analyze", headers=headers, json={"question":question})
        assert response.status_code == 200
        assert response.json()["data"]["intent"] == expected
        assert response.json()["data"]["calculated_data"] == []


def test_gemini_success_uses_snapshot_and_has_accurate_provider_label(monkeypatch):
    import backend.app.services as services
    from backend.app.config import get_settings
    from google import genai

    captured = {}
    class SuccessfulModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return type("Result", (), {"text":'{"interpretation":"The stored snapshot has open alerts.","recommendations":["Review the 3 stored open alerts."]}'})()
    class SuccessfulClient:
        def __init__(self, **kwargs): self.models = SuccessfulModels()

    settings = get_settings().model_copy(update={"gemini_api_key":"fake-configured-key"})
    monkeypatch.setattr(services, "get_settings", lambda: settings)
    monkeypatch.setattr(genai, "Client", SuccessfulClient)
    snapshot = {"readiness_pct":88,"alerts_open":3,"meta":{"generated_at":"2026-10-11T00:00:00Z"},
                "metric_basis":{"readiness_pct":{"numerator":42,"denominator":48,"basis":"true / explicit bool"}}}
    result = asyncio.run(services.analyze(snapshot, "What does the dashboard say about alerts?"))
    assert result["source"] == "gemini"
    assert result["source_label"] == "Gemini AI"
    assert result["provider"] == {"name":"Gemini","configured":True,"used":True}
    assert "What does the dashboard say about alerts?" in captured["contents"]
    assert "42" in captured["contents"] and "alerts_open" in captured["contents"]
    assert result["calculated_data"][0]["timestamp"] == "2026-10-11T00:00:00Z"
    assert "confidence" not in result


def test_aircraft_count_question_leads_with_current_snapshot_values_and_provenance(monkeypatch):
    import backend.app.services as services
    from backend.app.config import get_settings

    monkeypatch.setattr(services, "get_settings", lambda: get_settings().model_copy(update={"gemini_api_key":None}))
    snapshot = {
        "aircraft_total": 7,
        "readiness_pct": 75,
        "availability_pct": 67,
        "meta": {"generated_at": "2026-10-17T10:00:00Z"},
        "metric_basis": {
            "aircraft_total": {"basis": "unique aircraft records from current query"},
            "readiness_pct": {"numerator": 3, "denominator": 4, "basis": "explicit confirmation booleans"},
            "availability_pct": {"numerator": 2, "denominator": 3, "basis": "recognized availability statuses"},
        },
    }

    result = asyncio.run(services.analyze(snapshot, "How many aircraft do I have?"))
    assert result["summary"] == (
        "You have 7 aircraft records in the current dashboard snapshot. "
        "3 aircraft are readiness-confirmed (75%), and 2 aircraft are marked available (67%)."
    )
    assert result["source_label"] == "Rule-based fallback"
    assert result["provider"]["used"] is False
    aircraft_fact = result["calculated_data"][0]
    assert aircraft_fact["label"] == "Aircraft records"
    assert aircraft_fact["value"] == "7"
    assert aircraft_fact["basis"] == "unique aircraft records from current query"
    assert aircraft_fact["timestamp"] == snapshot["meta"]["generated_at"]


@pytest.mark.parametrize(("question", "metric", "expected"), [
    ("How many aircraft are available?", "availability_pct", "2 aircraft are marked available (67%)."),
    ("How many aircraft have readiness confirmation?", "readiness_pct", "3 aircraft are readiness-confirmed (75%)."),
])
def test_aircraft_availability_and_readiness_questions_answer_directly(monkeypatch, question, metric, expected):
    import backend.app.services as services
    from backend.app.config import get_settings

    monkeypatch.setattr(services, "get_settings", lambda: get_settings().model_copy(update={"gemini_api_key":None}))
    snapshot = {
        "aircraft_total": 7,
        "availability_pct": 67,
        "readiness_pct": 75,
        "metric_basis": {
            "availability_pct": {"numerator": 2, "denominator": 3, "basis": "recognized status"},
            "readiness_pct": {"numerator": 3, "denominator": 4, "basis": "explicit confirmation"},
        },
    }
    result = asyncio.run(services.analyze(snapshot, question))
    assert result["summary"] == expected
    assert result["source"] == "rules-fallback"
    assert result["provider"]["used"] is False
    assert any(fact["label"] == ("Aircraft availability" if metric == "availability_pct"
                                  else "Aircraft readiness confirmation") for fact in result["calculated_data"])


def test_aircraft_questions_report_missing_snapshot_data_without_inventing_values(monkeypatch):
    import backend.app.services as services
    from backend.app.config import get_settings

    monkeypatch.setattr(services, "get_settings", lambda: get_settings().model_copy(update={"gemini_api_key":None}))
    result = asyncio.run(services.analyze({"meta": {"generated_at": "2026-10-17T10:00:00Z"}},
                                          "How many aircraft do I have?"))
    assert result["summary"].startswith("The aircraft count is unavailable in the current dashboard snapshot")
    assert "None" not in result["summary"]
    assert "0 aircraft" not in result["summary"]
    assert result["calculated_data"][0]["value"].startswith("Unavailable:")
    assert "no valid records" in result["summary"]
    assert result["source_label"] == "Rule-based fallback"
    assert result["provider"]["used"] is False


@pytest.mark.parametrize("failure", ["connect", "timeout"])
def test_weather_provider_failure_does_not_use_persisted_demo_rows(monkeypatch, failure):
    import httpx
    import backend.app.services as services
    from backend.app.models import WeatherObservation

    class OfflineClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs):
            if failure == "timeout":
                raise httpx.ReadTimeout("provider timed out")
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **kwargs: OfflineClient())
    services._weather_cache.pop((12.3456, 45.6789), None)
    with SessionLocal() as db:
        demo_before = db.query(WeatherObservation).filter_by(source="demo").count()
        result = asyncio.run(services.fetch_weather(12.3456, 45.6789, db))
        demo_after = db.query(WeatherObservation).filter_by(source="demo").count()
    assert result["source"] == "unavailable"
    assert result["is_demo"] is False
    assert result["hourly"] == []
    assert "no saved provider records" in result["notice"]
    assert demo_before == demo_after == 24


@pytest.mark.parametrize("hourly", [
    None,
    {"time": "not-a-list"},
    {"time": ["2026-10-11T12:00"], "temperature_2m": []},
])
def test_weather_invalid_provider_payload_is_unavailable_not_live(monkeypatch, hourly):
    import backend.app.services as services
    from backend.app.models import WeatherObservation

    class ProviderResponse:
        def raise_for_status(self): pass
        def json(self): return {"hourly": hourly}

    class ProviderClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return ProviderResponse()

    monkeypatch.setattr(services.httpx, "AsyncClient", lambda **kwargs: ProviderClient())
    coordinates = (19.2345, 78.9876)
    services._weather_cache.pop(coordinates, None)
    with SessionLocal() as db:
        result = asyncio.run(services.fetch_weather(*coordinates, db))
        provider_rows = db.query(WeatherObservation).filter_by(
            source="open-meteo", latitude=coordinates[0], longitude=coordinates[1]
        ).count()
    assert result["source"] == "unavailable"
    assert result["is_demo"] is False
    assert result["hourly"] == []
    assert provider_rows == 0


def test_gemini_provider_error_uses_rule_fallback(monkeypatch):
    from backend.app.config import get_settings
    import backend.app.services as services
    from google import genai

    class BrokenModels:
        def generate_content(self, **kwargs): raise RuntimeError("quota exceeded")
    class BrokenClient:
        def __init__(self, **kwargs): self.models = BrokenModels()

    settings = get_settings().model_copy(update={"gemini_api_key":"fake-key"})
    monkeypatch.setattr(services, "get_settings", lambda: settings)
    monkeypatch.setattr(genai, "Client", BrokenClient)
    result = asyncio.run(services.analyze({"readiness_pct":90,"alerts_open":1}, "Analyze the dashboard snapshot"))
    assert result["source"] == "rules-fallback"
    assert result["source_label"] == "Rule-based fallback"
    assert result["provider"] == {"name":"Gemini","configured":True,"used":False}
    assert result["ai_generated"] is False
    assert "confidence" not in result


def test_authorized_crud_and_schedule_conflict(client):
    user = client.post("/api/auth/register", json={"email":"admin-demo@example.com", "password":"admin-demo-test-passphrase"}).json()["data"]
    with SessionLocal() as db:
        profile = db.scalar(select(Profile).where(Profile.email == "admin-demo@example.com"))
        profile.role = "admin"
        db.commit()
    headers = {"Authorization": f"Bearer {user['access_token']}"}

    aircraft = client.post("/api/aircraft", headers=headers, json={"resource_id":"TEST-A-900","category":"Training","availability_status":"available","maintenance_status":"clear","readiness_confirmation":True,"notes":"test"})
    assert aircraft.status_code == 201
    aircraft_id = aircraft.json()["data"]["id"]
    duplicate_aircraft = client.post("/api/aircraft", headers=headers, json={"resource_id":" test-a-900 ","category":"Training","availability_status":"available","maintenance_status":"clear"})
    assert duplicate_aircraft.status_code == 409
    assert client.patch(f"/api/aircraft/{aircraft_id}", headers=headers, json={"availability_status":"standby"}).json()["data"]["availability_status"] == "standby"

    crew = client.post("/api/crew", headers=headers, json={"display_id":"TEST-C-900","group_name":"Test group","readiness_status":"ready"})
    assert crew.status_code == 201
    assert client.patch(f"/api/crew/{crew.json()['data']['id']}", headers=headers, json={"readiness_status":"review"}).json()["data"]["readiness_status"] == "review"

    maintenance = client.post("/api/maintenance", headers=headers, json={"aircraft_id":aircraft_id,"title":"Test inspection","status":"in_progress","priority":"normal"})
    assert maintenance.status_code == 201
    maintenance_id = maintenance.json()["data"]["id"]
    assert client.patch(f"/api/maintenance/{maintenance_id}", headers=headers, json={"status":"completed"}).status_code == 200

    starts = datetime.now(timezone.utc) + timedelta(days=4)
    schedule_payload = {"title":"Test readiness review","category":"review","status":"scheduled","starts_at":starts.isoformat(),"ends_at":(starts+timedelta(hours=1)).isoformat()}
    assert client.post("/api/schedules", headers=headers, json=schedule_payload).status_code == 201
    conflicting = {**schedule_payload,"title":"Conflicting test review","starts_at":(starts+timedelta(minutes=20)).isoformat(),"ends_at":(starts+timedelta(minutes=40)).isoformat()}
    assert client.post("/api/schedules", headers=headers, json=conflicting).status_code == 409

    alert = client.post("/api/alerts", headers=headers, json={"severity":"low","category":"Testing","message":"Integration test alert"})
    assert alert.status_code == 201
    alert_id = alert.json()["data"]["id"]
    assert client.patch(f"/api/alerts/{alert_id}/acknowledge", headers=headers).json()["data"]["status"] == "acknowledged"
    assert client.patch(f"/api/alerts/{alert_id}/resolve", headers=headers).json()["data"]["status"] == "resolved"

    report = client.post("/api/reports", headers=headers, json={"report_type":"integration-test","filters":{}})
    assert report.status_code == 201
    assert "confirmed readiness" in report.json()["data"]["summary"]
    own_report_ids = {row["id"] for row in client.get("/api/reports", headers=headers).json()["data"]}
    assert report.json()["data"]["id"] in own_report_ids
    from backend.app.security import create_access_token
    with SessionLocal() as db:
        viewer_profile = Profile(email="report-viewer@example.com", password_hash="test-only", role="viewer")
        db.add(viewer_profile)
        db.flush()
        viewer_token = create_access_token(viewer_profile)
        db.commit()
    viewer_reports = client.get("/api/reports", headers={"Authorization":f"Bearer {viewer_token}"})
    assert viewer_reports.status_code == 200
    assert viewer_reports.json()["data"] == []
    assert client.delete(f"/api/crew/{crew.json()['data']['id']}", headers=headers).status_code == 204
    assert client.delete(f"/api/maintenance/{maintenance_id}", headers=headers).status_code == 204
    assert client.delete(f"/api/aircraft/{aircraft_id}", headers=headers).status_code == 204


def test_list_routes_and_weather_failure_shape(client, monkeypatch):
    import backend.app.main as main

    async def weather_offline(latitude, longitude, db):
        return {"source":"unavailable", "is_demo":False, "stale":False,
                "notice":"Open-Meteo is unavailable and no saved provider records exist.",
                "location":{"name":"Test location", "latitude":latitude,"longitude":longitude},
                "units":{}, "current":{}, "hourly":[], "fetched_at":None}

    monkeypatch.setattr(main, "fetch_weather", weather_offline)
    assert isinstance(client.get("/api/aircraft").json()["data"], list)
    assert isinstance(client.get("/api/crew").json()["data"], list)
    assert len(client.get("/api/activity").json()["data"]) > 0
    current = client.get("/api/weather/current").json()["data"]
    assert current["source"] == "unavailable"
    assert current["is_demo"] is False
    assert current["notice"]
    assert client.get("/api/weather/forecast").status_code == 503
    live = client.get("/api/analytics/trends?days=1").json()["data"]
    assert all(row["source"] == "open-meteo" for row in live["weather"])
    demo = client.get("/api/analytics/trends?days=1&mode=demo").json()["data"]
    assert demo["weather"] and demo["meta"]["source_label"] == "DEMO DATA"
    assert client.get("/api/analytics/trends?days=0").status_code == 422
    dashboard = client.get("/").text
    assert "AEROINTEL AI" in dashboard
    assert '/api/client-config.js' in dashboard
    assert 'class="card panel health-card"' not in dashboard
    assert "DB + weather checks" not in dashboard
    assert "SIMULATED OPS" not in dashboard
    assert "DEMO DATA" in dashboard
    assert "Demo aircraft records" not in dashboard
    assert "fictional demo data" in dashboard
    assert 'title="Demo data"' not in dashboard


def test_graph_source_separation_provenance_and_migration_preservation(client):
    from backend.app.models import WeatherObservation
    from backend.app.config import get_settings
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        demo_before = db.query(WeatherObservation).filter_by(source="demo").count()
        db.add_all([
            WeatherObservation(observed_at=now-timedelta(hours=1), location=get_settings().open_meteo_location,
                temperature_c=21.4, source="open-meteo", data_type="historical",
                latitude=get_settings().open_meteo_latitude, longitude=get_settings().open_meteo_longitude,
                fetched_at=now, created_at=now),
            WeatherObservation(observed_at=now+timedelta(hours=1), location=get_settings().open_meteo_location,
                temperature_c=22.1, source="open-meteo", data_type="forecast",
                latitude=get_settings().open_meteo_latitude, longitude=get_settings().open_meteo_longitude,
                fetched_at=now, created_at=now),
        ])
        db.commit()
        all_before_migration = db.query(WeatherObservation).count()

    # Running the additive migration a second time must preserve every existing row.
    main_module.migrate_weather_provenance()
    with SessionLocal() as db:
        assert db.query(WeatherObservation).filter_by(source="demo").count() == demo_before == 24
        assert db.query(WeatherObservation).count() == all_before_migration

    live = client.get("/api/analytics/trends?days=7").json()["data"]
    assert live["weather"]
    assert {row["source"] for row in live["weather"]} == {"open-meteo"}
    assert live["meta"]["source_label"] == "OPEN-METEO FORECAST DATA"
    assert {row["data_type"] for row in live["weather"]} <= {"forecast", "historical"}
    provider_row = next(row for row in live["weather"] if row["created_at"] is not None)
    assert provider_row["observation_timestamp"] == provider_row["time"]
    assert provider_row["latitude"] is not None and provider_row["longitude"] is not None
    assert provider_row["fetched_at"] is not None
    demo = client.get("/api/analytics/trends?days=7&mode=demo").json()["data"]
    assert demo["weather"] and {row["source"] for row in demo["weather"]} == {"demo"}
    assert demo["meta"]["source_label"] == "DEMO DATA"
    mixed = client.get("/api/analytics/trends?days=7&mode=all").json()["data"]
    assert {row["source"] for row in mixed["weather"]} == {"demo", "open-meteo"}
    assert mixed["meta"]["source_label"] == "MIXED SOURCES — NOT LIVE-ONLY"
    assert mixed["meta"]["warning"]


def test_operational_metric_formulas_unknown_statuses_duplicates_and_empty_data():
    from backend.app.services import calculate_operational_metrics

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    aircraft = [
        SimpleNamespace(id=1, resource_id=" A-1 ", updated_at=t0, availability_status="available", readiness_confirmation=False),
        SimpleNamespace(id=2, resource_id="a-1", updated_at=t0+timedelta(hours=1), availability_status="standby", readiness_confirmation=True),
        SimpleNamespace(id=3, resource_id="A-2", updated_at=t0, availability_status="maintenance", readiness_confirmation=False),
        SimpleNamespace(id=4, resource_id="A-3", updated_at=t0, availability_status="invalid", readiness_confirmation=None),
        SimpleNamespace(id=5, resource_id=None, updated_at=t0, availability_status=None, readiness_confirmation=True),
    ]
    crew = [
        SimpleNamespace(id=1, display_id=" C-1 ", confirmation_at=t0, readiness_status="review"),
        SimpleNamespace(id=2, display_id="c-1", confirmation_at=t0+timedelta(hours=1), readiness_status="ready"),
        SimpleNamespace(id=3, display_id="C-2", confirmation_at=t0, readiness_status="unknown"),
    ]
    metrics = calculate_operational_metrics(aircraft, crew)
    assert metrics["aircraft_total"] == 4
    assert metrics["availability"]["known_status_count"] == 2
    assert metrics["availability"]["unknown"] == 2
    assert metrics["availability"]["percentages"]["standby"] == 50
    assert metrics["availability"]["percentages"]["maintenance"] == 50
    assert metrics["readiness_pct"] == 67  # 2 true / 3 explicit booleans; half rounds up
    assert metrics["data_quality"]["duplicate_aircraft_records"] == 1
    assert metrics["data_quality"]["duplicate_crew_records"] == 1
    assert metrics["crew_readiness_pct"] == 100
    assert metrics["resource_utilization_pct"] is None

    empty = calculate_operational_metrics([], [])
    assert empty["aircraft_total"] == 0
    assert empty["readiness_pct"] is None
    assert empty["availability_pct"] is None
    assert empty["crew_readiness_pct"] is None
    assert empty["resource_utilization_pct"] is None


def test_normalized_business_ids_have_database_uniqueness_indexes():
    from sqlalchemy.exc import IntegrityError
    from backend.app.models import Aircraft, Crew

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(Aircraft(resource_id="INDEX-A-900", category="Test", availability_status="available", maintenance_status="clear"))
        db.commit()
        db.add(Aircraft(resource_id=" index-a-900 ", category="Test", availability_status="available", maintenance_status="clear"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Crew(display_id="INDEX-C-900", group_name="Test", readiness_status="ready"))
        db.commit()
        db.add(Crew(display_id=" index-c-900 ", group_name="Test", readiness_status="ready"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(Aircraft).filter(Aircraft.resource_id == "INDEX-A-900").count() == 1
        assert db.query(Crew).filter(Crew.display_id == "INDEX-C-900").count() == 1


def test_weather_provider_points_have_database_deduplication_constraint():
    from sqlalchemy.exc import IntegrityError
    from backend.app.models import WeatherObservation

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        before = db.query(WeatherObservation).count()
        values = dict(observed_at=now, location="Index test", source="open-meteo", data_type="forecast", latitude=1.2345, longitude=2.3456, fetched_at=now, created_at=now)
        db.add(WeatherObservation(**values))
        db.commit()
        db.add(WeatherObservation(**values))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(WeatherObservation).count() == before + 1


def test_public_inventory_import_is_idempotent_and_excludes_procurement(client):
    from backend.app.inventory import import_inventory, inventory_summary, load_dataset
    from backend.app.models import PublicInventoryRecord

    dataset = load_dataset()
    with SessionLocal() as db:
        db.query(PublicInventoryRecord).delete()
        db.commit()
        first = import_inventory(db, dataset)
        second = import_inventory(db, dataset)
        rows = list(db.query(PublicInventoryRecord).filter_by(dataset_version=dataset["dataset_version"]).all())
        summary = inventory_summary(rows, dataset)
    assert first["records"] == 62
    assert first["inserted"] == 62
    assert second["inserted"] == 0 and second["updated"] == 62
    assert summary["service_totals"] == {"Indian Air Force": 1699, "Indian Army Aviation": 367, "Indian Navy Aviation": 287}
    assert summary["inventory_total"] == 2353
    assert summary["procurement_total"] == 8
    assert summary["discrepancies"] == [
        {"scope":"Indian Air Force","reported_approximate":1700,"calculated_from_rows":1699,"difference":-1,"note":"Approximate aggregate versus supplied individual row sum; source counts are preserved."},
        {"scope":"All services","reported_approximate":2350,"calculated_from_rows":2353,"difference":3,"note":"Approximate aggregate versus supplied individual row sum; source counts are preserved."},
    ]
    procurement = next(row for row in rows if row.count_type == "procurement_order")
    assert procurement.is_active_inventory is False
    assert procurement.data_classification == "ESTIMATE"
    assert procurement.source_url == dataset["source_url"]


def test_public_inventory_api_filters_and_attributes_source(client):
    from backend.app.models import PublicInventoryRecord

    with SessionLocal() as db:
        db.query(PublicInventoryRecord).delete()
        db.commit()
    response = client.get("/api/public-inventory?organization=Indian%20Navy%20Aviation&category=Helicopters&model=Dhruv")
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["display_name"] == "Dhruv"
    assert body["data"][0]["estimated_count"] == 24
    assert body["data"][0]["count_type"] == "estimated_inventory"
    with SessionLocal() as db:
        assert db.query(PublicInventoryRecord).count() == 62
    assert body["meta"]["data_classification"] == "ESTIMATE"
    assert body["meta"]["source_url"] == "https://www.globalmilitary.net/air_forces/ind/"
    assert body["meta"]["source_date"].startswith("2026-10-10")
    assert "2,353 listed rows versus the rounded all-services estimate" in body["meta"]["source_note"]
    assert "not official operational readiness" in body["meta"]["notice"]


def test_ml_endpoints_expose_validated_synthetic_results(client):
    import backend.app.main as main

    status = client.get("/api/ml/status")
    summary = client.get("/api/ml/dataset-summary")
    assert status.status_code == summary.status_code == 200
    assert status.json()["notice"] == "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE"
    assert summary.json()["data"]["rows"] == 150
    assert summary.json()["data"]["class_distribution"] == {"Low": 56, "Medium": 75, "High": 19}
    assert client.get("/api/ml/evaluation").status_code == (200 if status.json()["data"]["available"] else 503)

    invalid = client.post("/api/ml/predict", json={"aircraft_category": "Transport"})
    assert invalid.status_code == 422
    payload = {
        "aircraft_category": "Transport", "propulsion_type": "Jet", "aircraft_age_years": 12,
        "total_flight_hours": 4200, "total_cycles": 2300, "days_since_last_maintenance": 45,
        "maintenance_events_last_12_months": 2, "unresolved_maintenance_items": 0,
        "inspection_overdue_flag": False,
    }
    prediction = client.post("/api/ml/predict", json=payload)
    assert prediction.status_code == (200 if status.json()["data"]["available"] else 503)
    if prediction.status_code == 200:
        assert prediction.json()["data"]["predicted_demo_label"] in {"Low", "Medium", "High"}
        assert prediction.json()["data"]["notice"] == "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE"

    for route in ("/api/ml/status", "/api/ml/dataset-summary", "/api/ml/evaluation"):
        assert client.get(route).status_code in {200, 503}


def test_ai_ml_questions_use_verified_backend_results_only(client, monkeypatch):
    import backend.app.main as main

    registration = client.post("/api/auth/register", json={
        "email": "ml-assistant@example.com", "password": "ml-assistant-test-passphrase",
    })
    assert registration.status_code == 201
    headers = {"Authorization": f"Bearer {registration.json()['data']['access_token']}"}

    async def unexpected_provider_call(*_args, **_kwargs):
        raise AssertionError("ML questions must not pass unverified content to the AI provider")

    monkeypatch.setattr(main, "analyze", unexpected_provider_call)
    response = client.post("/api/ai/analyze", headers=headers,
                          json={"question": "What are the model accuracy and class distribution?",
                                "context": {"accuracy": 1.0, "class_distribution": {"fake": 999}}})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == "ml-backend"
    assert data["verified"] is True
    assert data["provider"]["used"] is False
    assert data["ml_notice"] == "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE"
    assert any(item["value"] != "1.0" for item in data["calculated_data"])
    assert data["recommendations"] == []


def test_ml_analytics_page_and_warning_are_in_dashboard(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ML Analytics" in response.text
    assert "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE" in response.text
    assert "/api/ml/evaluation" in response.text
