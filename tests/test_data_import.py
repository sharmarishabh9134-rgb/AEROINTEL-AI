import uuid
from datetime import datetime, timedelta, timezone

from backend.app.database import SessionLocal
from backend.app.models import Aircraft, Profile, MaintenanceRecord, ScheduleRecord, Alert
from backend.app.security import create_access_token
from backend.app.data_import import inspect_upload, commit_rows
from test_api import client


def analyst_headers():
    with SessionLocal() as db:
        profile = Profile(email=f"import-{uuid.uuid4().hex}@example.com", password_hash="test-only", role="analyst")
        db.add(profile)
        db.commit()
        db.refresh(profile)
        return {"Authorization": f"Bearer {create_access_token(profile)}"}


def upload(client, headers, route, dataset_type, filename, content):
    data = {"dataset_type": dataset_type}
    if route.endswith("/commit"):
        data["confirm_import"] = "true"
    return client.post(route, headers=headers, data=data,
                       files={"file": (filename, content, "text/csv")})


def test_import_preview_confirm_duplicate_skip_and_dashboard_consistency(client):
    headers = analyst_headers()
    resource_id = f"LOAD-{uuid.uuid4().hex[:10].upper()}"
    csv = f"resource_id,category,availability_status,maintenance_status\n{resource_id},Transport,available,clear\n".encode()
    before = client.get("/api/dashboard/summary", headers=headers).json()["data"]["aircraft_total"]
    preview = upload(client, headers, "/api/imports/preview", "aircraft", "fleet.csv", csv)
    assert preview.status_code == 200
    assert preview.json()["data"]["will_import"] == 1
    committed = upload(client, headers, "/api/imports/commit", "aircraft", "fleet.csv", csv)
    assert committed.status_code == 201
    assert committed.json()["data"]["inserted"] == 1
    rows = client.get("/api/aircraft", headers=headers).json()["data"]
    assert any(row["resource_id"] == resource_id and row["is_demo"] is False and row["data_origin"] == "upload" for row in rows)
    after = client.get("/api/dashboard/summary", headers=headers).json()["data"]["aircraft_total"]
    assert after == before + 1
    duplicate = upload(client, headers, "/api/imports/preview", "aircraft", "fleet.csv", csv)
    assert duplicate.json()["data"]["will_skip"] == 1
    assert upload(client, headers, "/api/imports/commit", "aircraft", "fleet.csv", csv).json()["data"]["skipped"] == 1
    history = client.get("/api/imports/history", headers=headers).json()["data"]
    assert history[0]["dataset_type"] == "aircraft"
    assert history[0]["inserted"] == 0
    assert client.get("/api/health").json()["data"]["database_dialect"] == "sqlite"


def test_imports_support_maintenance_flights_and_alerts(client):
    headers = analyst_headers()
    aircraft_id = f"LINK-{uuid.uuid4().hex[:8].upper()}"
    with SessionLocal() as db:
        db.add(Aircraft(resource_id=aircraft_id, category="Transport", availability_status="available", maintenance_status="clear"))
        db.commit()
    maintenance = f"aircraft_resource_id,title,status\n{aircraft_id},Import inspection {uuid.uuid4().hex[:6]},open\n".encode()
    assert upload(client, headers, "/api/imports/commit", "maintenance", "maint.csv", maintenance).status_code == 201
    schedule_title = f"Import Flight {uuid.uuid4().hex[:6]}"
    starts_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    schedule = f"title,category,starts_at,aircraft_resource_id\n{schedule_title},Flight,{starts_at},{aircraft_id}\n".encode()
    assert upload(client, headers, "/api/imports/commit", "flights", "flights.csv", schedule).status_code == 201
    alert = f"severity,category,message\nlow,Import test,Import alert {uuid.uuid4().hex[:8]}\n".encode()
    assert upload(client, headers, "/api/imports/commit", "alerts", "alerts.csv", alert).status_code == 201
    assert any(row["title"].startswith("Import inspection") for row in client.get("/api/maintenance?days=30", headers=headers).json()["data"])
    assert any(row["title"] == schedule_title for row in client.get("/api/schedules?days=7", headers=headers).json()["data"])
    assert any(row["message"].startswith("Import alert") for row in client.get("/api/alerts?days=30", headers=headers).json()["data"])
    summary = client.get("/api/dashboard/summary", headers=headers).json()["data"]
    assert summary["maintenance_open"] > 0
    assert summary["schedule_active"] > 0
    assert summary["alerts_open"] > 0


def test_crew_csv_import_is_validated_deduplicated_and_visible_on_dashboard(client):
    headers = analyst_headers()
    display_id = f"CREW-{uuid.uuid4().hex[:8].upper()}"
    csv = ("display_id,group_name,readiness_status,display_name,role,license_rating,duty_hours,rest_hours,medical_expiry,currency_expiry\n"
           f"{display_id},Demo Flight,ready,Sample Crew,Flight crew,Type rated,4.5,12,2027-06-01T00:00:00Z,2027-05-01T00:00:00Z\n").encode()
    before = client.get("/api/dashboard/summary", headers=headers).json()["data"]["crew_total"]
    preview = upload(client, headers, "/api/imports/preview", "crew", "crew.csv", csv)
    assert preview.status_code == 200
    assert preview.json()["data"]["will_import"] == 1
    assert preview.json()["data"]["rejected"] == 0
    committed = upload(client, headers, "/api/imports/commit", "crew", "crew.csv", csv)
    assert committed.status_code == 201
    assert committed.json()["data"] == {
        "dataset_type": "crew", "inserted": 1, "skipped": 0, "rejected": 0, "failed": 0,
        "policy": "Insert new records; skip matches by business identifier/natural key. No updates are performed.",
    }
    rows = client.get("/api/crew", headers=headers).json()["data"]
    stored = next(row for row in rows if row["display_id"] == display_id)
    assert stored["duty_hours"] == 4.5
    assert stored["is_demo"] is False
    assert stored["data_origin"] == "upload"
    assert client.get("/api/dashboard/summary", headers=headers).json()["data"]["crew_total"] == before + 1
    duplicate = upload(client, headers, "/api/imports/preview", "crew", "crew.csv", csv)
    assert duplicate.json()["data"]["will_skip"] == 1
    assert upload(client, headers, "/api/imports/commit", "crew", "crew.csv", csv).json()["data"]["skipped"] == 1


def test_downloadable_templates_match_import_schema(client):
    import csv
    import io
    from backend.app.data_import import SCHEMAS

    for dataset_type, schema in SCHEMAS.items():
        response = client.get(f"/api/imports/templates/{dataset_type}.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert next(csv.reader(io.StringIO(response.text))) == schema["fields"]


def test_commit_requires_explicit_confirmation(client):
    headers = analyst_headers()
    resource_id = f"CONFIRM-{uuid.uuid4().hex[:8].upper()}"
    content = f"resource_id,category,availability_status,maintenance_status\n{resource_id},Transport,available,clear\n".encode()
    response = client.post("/api/imports/commit", headers=headers, data={"dataset_type": "aircraft"},
                           files={"file": ("aircraft.csv", content, "text/csv")})
    assert response.status_code == 400
    rows = client.get("/api/aircraft", headers=headers).json()["data"]
    assert all(row["resource_id"] != resource_id for row in rows)


def test_import_rejects_invalid_files_missing_columns_and_dates(client):
    headers = analyst_headers()
    invalid = upload(client, headers, "/api/imports/preview", "aircraft", "program.py", b"print(1)")
    assert invalid.status_code == 422
    missing = upload(client, headers, "/api/imports/preview", "aircraft", "partial.csv", b"resource_id,category\nA-01,Transport\n")
    assert missing.status_code == 200
    assert missing.json()["data"]["error_count"] > 0
    bad_date = upload(client, headers, "/api/imports/preview", "flights", "dates.csv",
                      b"title,category,starts_at\nTest Flight,Flight,not-a-date\n")
    assert bad_date.json()["data"]["error_count"] == 1
    bad_crew_date = upload(client, headers, "/api/imports/preview", "crew", "crew.csv",
                           b"display_id,group_name,readiness_status,medical_expiry\nCREW-BAD,Demo,ready,not-a-date\n")
    assert bad_crew_date.json()["data"]["rejected"] == 1


def test_import_unknown_aircraft_reference_is_rejected_before_commit(client):
    headers = analyst_headers()
    title = f"Rollback task {uuid.uuid4().hex[:8]}"
    first = f"title,category,starts_at,aircraft_resource_id\n{title},Flight,2027-02-01T10:00:00Z,\n".encode()
    unknown_id = f"MISSING-{uuid.uuid4().hex[:6]}"
    second = f"Rollback second,Flight,2027-02-02T10:00:00Z,{unknown_id}\n".encode()
    combined = b"title,category,starts_at,aircraft_resource_id\n" + first.splitlines()[1] + b"\n" + second
    response = upload(client, headers, "/api/imports/commit", "flights", "rollback.csv", combined)
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(ScheduleRecord).filter(ScheduleRecord.title == title).count() == 0


def test_import_transaction_rolls_back_records_inserted_before_a_commit_failure():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.models import Base

    isolated_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(isolated_engine)
    IsolatedSession = sessionmaker(bind=isolated_engine, expire_on_commit=False)
    with IsolatedSession() as db:
        aircraft = Aircraft(resource_id=f"ROLL-{uuid.uuid4().hex[:8]}", category="Transport",
                            availability_status="available", maintenance_status="clear")
        db.add(aircraft)
        db.commit()
        resource_id = aircraft.resource_id
        title = f"Atomic rollback {uuid.uuid4().hex[:8]}"
        csv = ("title,category,starts_at,aircraft_resource_id\n" +
               f"{title},Flight,2027-03-01T10:00:00Z,\n" +
               f"Second flight,Flight,2027-03-02T10:00:00Z,{resource_id}\n").encode()
        inspected = inspect_upload("rollback.csv", csv, "flights", db=db)
        db.delete(aircraft)
        db.commit()
        profile = Profile(email=f"rollback-{uuid.uuid4().hex}@example.com", password_hash="test-only", role="analyst")
        db.add(profile)
        db.commit()
        try:
            commit_rows(db, profile.id, inspected)
        except ValueError:
            pass
        else:
            raise AssertionError("The removed aircraft reference should fail the commit.")
        assert db.query(ScheduleRecord).filter(ScheduleRecord.title == title).count() == 0
    isolated_engine.dispose()


def test_import_authorization_and_upload_size_limit(client):
    viewer_headers = client.headers
    saved_auth = viewer_headers.pop("Authorization", None)
    anonymous = upload(client, viewer_headers, "/api/imports/preview", "aircraft", "fleet.csv",
                       b"resource_id,category,availability_status,maintenance_status\nANON-01,Transport,available,clear\n")
    assert anonymous.status_code == 401
    if saved_auth:
        viewer_headers["Authorization"] = saved_auth
    response = upload(client, viewer_headers, "/api/imports/preview", "aircraft", "fleet.csv",
                      b"resource_id,category,availability_status,maintenance_status\nAUTH-01,Transport,available,clear\n")
    assert response.status_code == 200
    assert response.json()["data"]["will_import"] == 1
    committed = upload(client, viewer_headers, "/api/imports/commit", "aircraft", "fleet.csv",
                       b"resource_id,category,availability_status,maintenance_status\nAUTH-01,Transport,available,clear\n")
    assert committed.status_code == 201
    assert client.get("/api/imports/history", headers=viewer_headers).status_code == 200
    headers = analyst_headers()
    too_big = b"resource_id,category,availability_status,maintenance_status\n" + b"A" * (5 * 1024 * 1024 + 1)
    response = upload(client, headers, "/api/imports/preview", "aircraft", "large.csv", too_big)
    assert response.status_code == 413
