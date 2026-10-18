import json
import logging
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from backend.app.config import get_settings
from backend.app.database import Base, engine, get_db
from backend.app.models import (Aircraft, Alert, AnalysisReport, AuditLog, Crew, MaintenanceRecord, Profile, PublicInventoryRecord, ScheduleRecord, WeatherObservation)
from backend.app.schemas import (AlertCreate, AircraftInput, AircraftUpdate, AnalyzeInput, CrewInput, CrewUpdate, LoginInput, MaintenanceInput, MaintenanceUpdate, ReportCreate, ScheduleInput, ScheduleUpdate, UserCreate, UserOut)
from backend.app.security import bearer, create_access_token, current_user, get_jwt_secret, password_hash, require_roles
from backend.app.services import analyze, calculate_operational_metrics, classify_analyzer_intent, dashboard_summary, fetch_weather, rows_as_dict, trend_series
from backend.app.inventory import filtered_inventory, import_inventory, inventory_summary, load_dataset
from backend.app.official_disclosures import filtered_official_disclosures, load_official_disclosures
from backend.app.seed import remove_seeded_demo_data, seed_demo_data
from backend.app.ml_analytics import (NOTICE as ML_NOTICE, assistant_results as ml_assistant_results,
                                      dataset_summary as ml_dataset_summary,
                                      evaluation_metrics as ml_evaluation_metrics,
                                      model_status as ml_model_status, predict as ml_predict)
from backend.app.schemas import MaintenanceDemoPrediction
from backend.app.fleet_intelligence import router as fleet_intelligence_router
from backend.app.aviation_public_data import fetch_public_aviation_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("aerointel")
settings = get_settings()


def migrate_weather_provenance():
    """Add weather provenance columns in place; existing rows are retained."""
    if engine.dialect.name != "sqlite":
        return
    if "weather_observations" not in inspect(engine).get_table_names():
        return
    columns = {column["name"] for column in inspect(engine).get_columns("weather_observations")}
    column_types = {
        "data_type": "VARCHAR(20)",
        "latitude": "FLOAT",
        "longitude": "FLOAT",
        "fetched_at": "DATETIME",
        "created_at": "DATETIME",
    }
    with engine.begin() as connection:
        for name, sql_type in column_types.items():
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE weather_observations ADD COLUMN {name} {sql_type}")
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        records = db.scalars(select(WeatherObservation)).all()
        for record in records:
            if not record.data_type:
                if record.source == "demo":
                    record.data_type = "demo"
                elif record.source == "open-meteo":
                    observed = record.observed_at
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    record.data_type = "forecast" if observed > now else "historical"
                else:
                    record.data_type = "unknown"
            # These legacy provider rows were fetched for the configured location.
            # Do not assign coordinates to demo rows or rows for another location.
            if (record.source == "open-meteo" and record.location == settings.open_meteo_location
                    and record.latitude is None and record.longitude is None):
                record.latitude = settings.open_meteo_latitude
                record.longitude = settings.open_meteo_longitude
        db.commit()
    # A provider fetch can race with another request. Enforce idempotency in
    # SQLite as well as in the fetcher's read-before-write logic.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_weather_source_location_observed_at "
            "ON weather_observations (source, latitude, longitude, observed_at)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_aircraft_resource_id_normalized "
            "ON aircraft (lower(trim(resource_id)))"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_crew_display_id_normalized "
            "ON crew (lower(trim(display_id)))"
        )


def migrate_dashboard_demo_fields():
    """Apply additive SQLite-only fields; PostgreSQL uses reviewed migration 006."""
    if engine.dialect.name != "sqlite":
        return
    additions = {
        "aircraft": {
            "registration": "VARCHAR(24)", "aircraft_type": "VARCHAR(140)", "base_airport": "VARCHAR(4)",
            "next_maintenance_due": "DATETIME", "hours_since_check": "FLOAT",
            "is_demo": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "crew": {
            "display_name": "VARCHAR(100)", "role": "VARCHAR(60)",
            "license_rating": "VARCHAR(120)", "duty_hours": "FLOAT",
            "rest_hours": "FLOAT", "medical_expiry": "DATETIME",
            "currency_expiry": "DATETIME", "is_demo": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "maintenance_records": {"is_demo": "BOOLEAN NOT NULL DEFAULT 0"},
        "schedule_records": {
            "aircraft_id": "INTEGER REFERENCES aircraft(id) ON DELETE SET NULL",
            "priority": "VARCHAR(20) NOT NULL DEFAULT 'normal'", "assignee": "VARCHAR(100)",
            "origin_icao": "VARCHAR(4)", "destination_icao": "VARCHAR(4)",
            "is_demo": "BOOLEAN NOT NULL DEFAULT 0",
        },
        "alerts": {"is_read": "BOOLEAN NOT NULL DEFAULT 0", "is_demo": "BOOLEAN NOT NULL DEFAULT 0"},
        "analysis_reports": {"is_demo": "BOOLEAN NOT NULL DEFAULT 0"},
    }
    with engine.begin() as connection:
        existing_tables = set(inspect(engine).get_table_names())
        for table, columns in additions.items():
            if table not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspect(engine).get_columns(table)}
            for name, sql_type in columns.items():
                if name not in existing_columns:
                    connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {sql_type}')


def initialize_database():
    is_production = settings.app_env.lower() in {"production", "prod"}
    get_jwt_secret()
    if settings.demo_mode and engine.dialect.name != "sqlite":
        raise RuntimeError("DEMO_MODE may only be used with local SQLite; it must not seed a PostgreSQL database.")
    if is_production and engine.dialect.name != "postgresql":
        raise RuntimeError("Production DATABASE_URL must point to PostgreSQL; SQLite is supported only for local development and tests.")
    if engine.dialect.name == "sqlite" and not is_production:
        Base.metadata.create_all(bind=engine)
        migrate_dashboard_demo_fields()
        migrate_weather_provenance()
    else:
        existing = set(inspect(engine).get_table_names())
        required = set(Base.metadata.tables)
        missing = sorted(required - existing)
        if missing:
            raise RuntimeError("Configured PostgreSQL is missing required tables: " + ", ".join(missing) + ". Inspect the schema and apply reviewed migrations before startup.")
        drift = []
        schema = inspect(engine)
        for table, model in Base.metadata.tables.items():
            actual = {column["name"] for column in schema.get_columns(table)}
            missing_columns = sorted(set(model.columns.keys()) - actual)
            if missing_columns:
                drift.append(f"{table} ({', '.join(missing_columns)})")
        if drift:
            raise RuntimeError("Configured PostgreSQL is missing model columns: " + "; ".join(drift) + ". Inspect the schema and apply reviewed migrations before startup.")
        required_indexes = {
            "weather_observations": {"uq_weather_source_location_observed_at"},
            "aircraft": {"uq_aircraft_resource_id_normalized"},
            "crew": {"uq_crew_display_id_normalized"},
        }
        missing_indexes = []
        for table, expected_names in required_indexes.items():
            actual_names = {item.get("name") for item in schema.get_indexes(table)}
            missing_indexes.extend(f"{table}.{name}" for name in sorted(expected_names - actual_names))
        if missing_indexes:
            raise RuntimeError("Configured PostgreSQL is missing required indexes: " + ", ".join(missing_indexes) + ". Inspect duplicate business IDs and apply reviewed migrations 004 and 005 before startup.")
    if settings.demo_mode:
        with Session(engine) as db:
            if settings.auto_seed_demo_data and not is_production:
                seed_demo_data(db)
            else:
                remove_seeded_demo_data(db)


@asynccontextmanager
async def lifespan(_app):
    initialize_database()
    yield


is_production = settings.app_env.lower() in {"production", "prod"}
app = FastAPI(title="AEROINTEL AI API", version="1.0.0", description="Administrative air-operations demo and decision-support API. All sample operational records are fictional.", lifespan=lifespan, docs_url=None if is_production else "/docs", redoc_url=None, openapi_url=None if is_production else "/openapi.json")

def cors_origins_for(config, production: bool) -> set[str]:
    local_origins = {
        "http://127.0.0.1:8000", "http://localhost:8000",
        "http://127.0.0.1:5500", "http://localhost:5500",
    }
    if production:
        return {config.frontend_url} if config.frontend_url and config.frontend_url not in local_origins else set()
    return local_origins | ({config.frontend_url} if config.frontend_url else set())


origins = cors_origins_for(settings, is_production)
app.add_middleware(CORSMiddleware, allow_origins=sorted(origins), allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])
app.include_router(fleet_intelligence_router)

_attempts: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request, key: str, maximum: int = 10, window_seconds: int = 60):
    now = time.monotonic()
    bucket = _attempts[key + ":" + (request.client.host if request.client else "unknown")]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= maximum:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again shortly.")
    bucket.append(now)


def operational_read_access(credentials=Depends(bearer), db: Session = Depends(get_db)):
    # The dashboard is public and read-only. If a caller supplies a token, still
    # validate it so expired or forged credentials are not silently accepted.
    return current_user(credentials, db) if credentials else None


def selected_date_window(days: int) -> tuple[datetime, datetime]:
    if days not in {1, 7, 30}:
        raise HTTPException(status_code=422, detail="days must be 1, 7, or 30")
    now = datetime.now(timezone.utc)
    return now - timedelta(days=days), now + timedelta(days=days)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    logger.error("Database request failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database is temporarily unavailable."})


@app.get("/", include_in_schema=False)
def home():
    path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Dashboard file not found")
    return FileResponse(path)


@app.get("/fleet", include_in_schema=False)
def fleet_dashboard():
    path = Path(__file__).resolve().parents[2] / "frontend" / "fleet.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Fleet dashboard file not found")
    return FileResponse(path)


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db_ok = False
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except SQLAlchemyError:
        pass
    return {"data": {"status": "ok" if db_ok else "degraded", "database": "connected" if db_ok else "unavailable", "mode": "demo" if settings.demo_mode else "configured", "weather": "configured" if settings.open_meteo_latitude is not None else "unconfigured", "ai": "configured" if settings.gemini_api_key else "fallback-enabled", "checked_at": datetime.now(timezone.utc).isoformat()}}


@app.get("/api/client-config.js", include_in_schema=False)
def client_config():
    import json
    value = json.dumps({"apiBaseUrl": settings.public_api_base_url}, separators=(",", ":"))
    return Response(f"window.AEROINTEL_CONFIG = Object.freeze({value});", media_type="application/javascript", headers={"Cache-Control": "no-store"})


@app.post("/api/auth/register", status_code=201)
def register(payload: UserCreate, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "register", 5)
    email = str(payload.email).lower()
    if db.scalar(select(Profile).where(Profile.email == email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    profile = Profile(email=email, password_hash=password_hash.hash(payload.password), role="viewer")
    db.add(profile)
    db.flush()
    db.add(AuditLog(profile_id=profile.id, action="account.registered", details="Self-service registration assigned viewer role"))
    db.commit()
    db.refresh(profile)
    return {"data": {"access_token": create_access_token(profile), "token_type": "bearer", "user": UserOut.model_validate(profile).model_dump()}}


@app.post("/api/auth/login")
def login(payload: LoginInput, request: Request, db: Session = Depends(get_db)):
    rate_limit(request, "login", 10)
    profile = db.scalar(select(Profile).where(Profile.email == str(payload.email).lower()))
    if not profile or not password_hash.verify(payload.password, profile.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    return {"data": {"access_token": create_access_token(profile), "token_type": "bearer", "user": UserOut.model_validate(profile).model_dump()}}


@app.post("/api/auth/logout")
def logout(profile: Profile = Depends(current_user), db: Session = Depends(get_db)):
    db.add(AuditLog(profile_id=profile.id, action="session.logout"))
    db.commit()
    return {"data": {"message": "Signed out. Discard the access token on the client."}}


@app.get("/api/auth/me")
def me(profile: Profile = Depends(current_user)):
    return {"data": UserOut.model_validate(profile).model_dump()}


@app.get("/api/dashboard/summary")
def summary(_profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    result = dashboard_summary(db)
    dataset = load_dataset()
    rows = dataset["records"]
    active_rows = [row for row in rows
                   if row.get("count_type") != "procurement_order" and row.get("is_active_inventory", True)]
    listed_total = sum(int(row["estimated_count"]) for row in active_rows)
    organizations = sorted({row["organization"] for row in active_rows})
    categories = sorted({row["category"] for row in active_rows})
    service_totals = {
        organization: sum(int(row["estimated_count"]) for row in active_rows
                          if row["organization"] == organization)
        for organization in organizations
    }
    category_totals = {
        category: sum(int(row["estimated_count"]) for row in active_rows
                      if row["category"] == category)
        for category in categories
    }
    top_types = sorted(active_rows, key=lambda row: (-int(row["estimated_count"]),
                                                     row["organization"], row["display_name"]))[:5]
    result["public_inventory_estimate"] = {
        "reported_total": dataset["reported_approximate_totals"]["All services"],
        "listed_row_total": listed_total,
        "model_rows": len(active_rows),
        "service_totals": service_totals,
        "category_totals": category_totals,
        "top_types": [{key: row[key] for key in ("organization", "display_name", "category", "estimated_count")}
                      for row in top_types],
        "source_date": dataset["source_date"],
        "source_name": dataset["source_name"],
        "source_url": dataset["source_url"],
        "source_note": dataset.get("source_note"),
    }
    return {"data": result}


@app.get("/api/public-inventory")
def public_inventory(
    organization: str | None = Query(None, max_length=80),
    category: str | None = Query(None, max_length=60),
    model: str | None = Query(None, max_length=100),
    _profile: Profile = Depends(operational_read_access),
    db: Session = Depends(get_db),
):
    dataset = load_dataset()
    current = db.scalars(select(PublicInventoryRecord).where(PublicInventoryRecord.dataset_version == dataset["dataset_version"])).all()
    if len(current) < len(dataset["records"]):
        import_inventory(db, dataset)
        current = db.scalars(select(PublicInventoryRecord).where(PublicInventoryRecord.dataset_version == dataset["dataset_version"])).all()
    filtered = filtered_inventory(db, organization, category, model, dataset["dataset_version"])
    current = [row for row in current if row.dataset_version == dataset["dataset_version"]]
    last_import = max((row.last_imported_at for row in current), default=None)
    summary = inventory_summary(current, dataset)
    return {"data": rows_as_dict(filtered), "summary": summary, "meta": {
        "dataset_version": dataset["dataset_version"],
        "source_name": dataset["source_name"],
        "source_url": dataset["source_url"],
        "source_note": dataset.get("source_note"),
        "source_date": dataset["source_date"],
        "last_imported_at": last_import.isoformat() if last_import else None,
        "data_classification": "ESTIMATE",
        "notice": "Open-source estimate — not official operational readiness. Inventory is not availability, serviceability, or live tracking.",
        "filters": {"organization": organization, "category": category, "model": model},
    }}


@app.get("/api/official-disclosures")
def official_aviation_disclosures(
    service: str | None = Query(None, max_length=80),
    q: str | None = Query(None, max_length=120),
    _profile: Profile = Depends(operational_read_access),
):
    """Return attributed public procurement disclosures; never merge them into active inventory."""
    dataset = load_official_disclosures()
    if service and service not in {"Indian Air Force", "Indian Army Aviation", "Indian Navy Aviation"}:
        raise HTTPException(status_code=422, detail="Unknown service filter")
    rows = [
        {
            **row,
            "data_origin": dataset["data_origin"],
            "dataset_version": dataset["dataset_version"],
            "imported_at": dataset["imported_at"],
            "terms_note": dataset["terms_note"],
        }
        for row in filtered_official_disclosures(dataset, service, q)
    ]
    return {
        "data": rows,
        "summary": {"record_count": len(rows), "total_disclosures": len(dataset["records"])},
        "meta": {
            "data_origin": dataset["data_origin"],
            "source_name": dataset["source_name"],
            "dataset_version": dataset["dataset_version"],
            "imported_at": dataset["imported_at"],
            "terms_note": dataset["terms_note"],
            "limitation": dataset["limitation"],
            "filters": {"service": service, "q": q},
        },
    }


@app.get("/api/aircraft")
def aircraft(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    # Aircraft availability is a current fleet snapshot, not an event stream.
    # Keep older aircraft records visible when the user changes the event range.
    selected_date_window(days)  # validate the shared range selector
    query = select(Aircraft).order_by(Aircraft.resource_id)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "days": days, "scope": "current fleet snapshot", "timezone": "UTC", "data_mode": "demo" if settings.demo_mode else "database"}}


@app.post("/api/aircraft", status_code=201)
def create_aircraft(payload: AircraftInput, profile: Profile = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    if db.scalar(select(Aircraft.id).where(Aircraft.resource_id == payload.resource_id)):
        raise HTTPException(status_code=409, detail="That resource ID already exists")
    row = Aircraft(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That resource ID already exists") from exc
    db.add(AuditLog(profile_id=profile.id, action="aircraft.created", details=row.resource_id))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.patch("/api/aircraft/{aircraft_id}")
def update_aircraft(aircraft_id: int, payload: AircraftUpdate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    row = db.get(Aircraft, aircraft_id)
    if not row: raise HTTPException(status_code=404, detail="Aircraft record not found")
    changes = {k:v for k,v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for key, value in changes.items(): setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(profile_id=profile.id, action="aircraft.updated", details=row.resource_id))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.delete("/api/aircraft/{aircraft_id}", status_code=204)
def delete_aircraft(aircraft_id: int, profile: Profile = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    row = db.get(Aircraft, aircraft_id)
    if not row: raise HTTPException(status_code=404, detail="Aircraft record not found")
    db.add(AuditLog(profile_id=profile.id, action="aircraft.deleted", details=row.resource_id))
    for maintenance_row in db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.aircraft_id == row.id)).all():
        maintenance_row.aircraft_id = None
    db.delete(row)
    db.commit()


@app.get("/api/crew")
def crew(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    # Crew readiness is a current roster snapshot; an old confirmation must not
    # hide a crew member from the roster or make the page appear empty.
    selected_date_window(days)  # validate the shared range selector
    query = select(Crew).order_by(Crew.display_id)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "days": days, "date_field": "confirmation_at", "timezone": "UTC"}}


@app.post("/api/crew", status_code=201)
def create_crew(payload: CrewInput, profile: Profile = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    if db.scalar(select(Crew.id).where(Crew.display_id == payload.display_id)):
        raise HTTPException(status_code=409, detail="That crew ID already exists")
    row = Crew(**payload.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That crew ID already exists") from exc
    db.add(AuditLog(profile_id=profile.id, action="crew.created", details=row.display_id))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.patch("/api/crew/{crew_id}")
def update_crew(crew_id: int, payload: CrewUpdate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    row = db.get(Crew, crew_id)
    if not row: raise HTTPException(status_code=404, detail="Crew record not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is not None: setattr(row, key, value)
    db.add(AuditLog(profile_id=profile.id, action="crew.updated", details=row.display_id))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.delete("/api/crew/{crew_id}", status_code=204)
def delete_crew(crew_id: int, profile: Profile = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    row = db.get(Crew, crew_id)
    if not row: raise HTTPException(status_code=404, detail="Crew record not found")
    db.add(AuditLog(profile_id=profile.id, action="crew.deleted", details=row.display_id))
    db.delete(row)
    db.commit()


@app.get("/api/maintenance")
def maintenance(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    since, _ = selected_date_window(days)
    # Pending maintenance remains operationally relevant even if it was opened
    # before the selected reporting window; completed history follows the range.
    query = select(MaintenanceRecord).where(
        or_(MaintenanceRecord.status.in_(("open", "in_progress", "overdue")), MaintenanceRecord.created_at >= since)
    ).order_by(MaintenanceRecord.created_at.desc())
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "days": days, "date_field": "created_at", "timezone": "UTC"}}


@app.post("/api/maintenance", status_code=201)
def create_maintenance(payload: MaintenanceInput, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    if payload.aircraft_id is not None and not db.get(Aircraft, payload.aircraft_id):
        raise HTTPException(status_code=404, detail="Linked aircraft record not found")
    row = MaintenanceRecord(**payload.model_dump())
    db.add(row)
    db.flush()
    if row.aircraft_id is not None and row.status in {"open", "in_progress", "overdue"}:
        aircraft_row = db.get(Aircraft, row.aircraft_id)
        aircraft_row.maintenance_status = "in_progress" if row.status == "in_progress" else "open"
        aircraft_row.availability_status = "maintenance"
    db.add(AuditLog(profile_id=profile.id, action="maintenance.created", details=row.title))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.patch("/api/maintenance/{record_id}")
def update_maintenance(record_id: int, payload: MaintenanceUpdate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    row = db.get(MaintenanceRecord, record_id)
    if not row: raise HTTPException(status_code=404, detail="Maintenance record not found")
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items(): setattr(row, key, value)
    if row.aircraft_id is not None and "status" in changes:
        aircraft_row = db.get(Aircraft, row.aircraft_id)
        if aircraft_row:
            active_maintenance = row.status in {"open", "in_progress", "overdue"}
            aircraft_row.maintenance_status = "in_progress" if row.status == "in_progress" else "open" if active_maintenance else "clear"
            if not active_maintenance: aircraft_row.availability_status = "available"
    db.add(AuditLog(profile_id=profile.id, action="maintenance.updated", details=row.title))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.delete("/api/maintenance/{record_id}", status_code=204)
def delete_maintenance(record_id: int, profile: Profile = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    row = db.get(MaintenanceRecord, record_id)
    if not row: raise HTTPException(status_code=404, detail="Maintenance record not found")
    db.add(AuditLog(profile_id=profile.id, action="maintenance.deleted", details=row.title))
    db.delete(row)
    db.commit()


@app.get("/api/schedules")
def schedules(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    since, until = selected_date_window(days)
    query = select(ScheduleRecord).where(ScheduleRecord.starts_at >= since, ScheduleRecord.starts_at <= until).order_by(ScheduleRecord.starts_at)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "days": days, "date_field": "starts_at", "window": "past and future", "timezone": "UTC"}}


@app.get("/api/schedules/conflicts")
def schedule_conflicts(starts_at: datetime = Query(...), ends_at: datetime = Query(...), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    if starts_at.tzinfo is None or ends_at.tzinfo is None or ends_at <= starts_at:
        raise HTTPException(status_code=422, detail="Provide a valid timezone-aware start and end time")
    overlapping = db.scalars(select(ScheduleRecord).where(ScheduleRecord.status.not_in(["complete", "cancelled"]), ScheduleRecord.starts_at < ends_at, ScheduleRecord.ends_at > starts_at).order_by(ScheduleRecord.starts_at)).all()
    return {"data": rows_as_dict(overlapping), "meta": {"conflict_count": len(overlapping)}}


@app.post("/api/schedules", status_code=201)
def create_schedule(payload: ScheduleInput, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    if payload.ends_at is None:
        raise HTTPException(status_code=422, detail="ends_at is required to validate schedule conflicts")
    overlaps = db.scalars(select(ScheduleRecord).where(ScheduleRecord.status.not_in(["complete", "cancelled"]), ScheduleRecord.starts_at < payload.ends_at, ScheduleRecord.ends_at > payload.starts_at)).all()
    if overlaps:
        raise HTTPException(status_code=409, detail={"message":"This schedule conflicts with existing items", "conflicts":rows_as_dict(overlaps)})
    row = ScheduleRecord(**payload.model_dump())
    db.add(row)
    db.flush()
    db.add(AuditLog(profile_id=profile.id, action="schedule.created", details=row.title))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.patch("/api/schedules/{schedule_id}")
def update_schedule(schedule_id: int, payload: ScheduleUpdate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    row = db.get(ScheduleRecord, schedule_id)
    if not row: raise HTTPException(status_code=404, detail="Schedule record not found")
    changes = {k:v for k,v in payload.model_dump(exclude_unset=True).items() if v is not None}
    starts = changes.get("starts_at", row.starts_at)
    ends = changes.get("ends_at", row.ends_at)
    if starts.tzinfo is None: starts = starts.replace(tzinfo=timezone.utc)
    if ends is not None and ends.tzinfo is None: ends = ends.replace(tzinfo=timezone.utc)
    if ends is not None and ends <= starts:
        raise HTTPException(status_code=422, detail="Schedule times must be timezone-aware and end after start")
    if ends is not None and changes.get("status", row.status) not in {"complete", "cancelled"}:
        overlaps = db.scalars(select(ScheduleRecord).where(ScheduleRecord.id != row.id, ScheduleRecord.status.not_in(["complete", "cancelled"]), ScheduleRecord.starts_at < ends, ScheduleRecord.ends_at > starts)).all()
        if overlaps: raise HTTPException(status_code=409, detail={"message":"This schedule conflicts with existing items", "conflicts":rows_as_dict(overlaps)})
    for key, value in changes.items(): setattr(row, key, value)
    db.add(AuditLog(profile_id=profile.id, action="schedule.updated", details=row.title))
    db.commit()
    db.refresh(row)
    return {"data": rows_as_dict([row])[0]}


@app.get("/api/alerts")
def alerts(status: str | None = Query(None, pattern="^(open|acknowledged|resolved)$"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    since, _ = selected_date_window(days)
    query = select(Alert).order_by(Alert.created_at.desc())
    query = query.where(Alert.created_at >= since)
    if status:
        query = query.where(Alert.status == status)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "days": days, "date_field": "created_at", "timezone": "UTC"}}


@app.get("/api/public-aviation-data")
async def public_aviation_data(_profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    """Live public weather and coarse flight count; external outages are non-fatal."""
    airports = [row[0] for row in db.query(Aircraft.base_airport).filter(Aircraft.base_airport.is_not(None)).distinct().limit(10).all()]
    result = await fetch_public_aviation_data(airports)
    fallback = {"is_demo": True, "data_origin": "SYNTHETIC_DEMO_FALLBACK"}
    if result["data"]["metar_source"] == "unavailable":
        observations = db.scalars(select(WeatherObservation).where(WeatherObservation.source == "demo")
                                  .order_by(WeatherObservation.observed_at.desc()).limit(24)).all()
        fallback["weather_observations"] = rows_as_dict(observations)
    if result["data"]["opensky_source"] == "unavailable":
        fallback["scheduled_demo_flights"] = db.scalar(select(func.count()).select_from(ScheduleRecord).where(
            ScheduleRecord.is_demo.is_(True), ScheduleRecord.category.ilike("%flight%")
        )) or 0
    if "weather_observations" in fallback or "scheduled_demo_flights" in fallback:
        result["data"]["fallback"] = fallback
        result["notices"].append("Showing labelled synthetic fallback records where a public feed is unavailable; these are not live observations.")
    return result


@app.post("/api/alerts", status_code=201)
def create_alert(payload: AlertCreate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    alert = Alert(severity=payload.severity, category=payload.category, message=payload.message)
    db.add(alert)
    db.add(AuditLog(profile_id=profile.id, action="alert.created", details=payload.category))
    db.commit()
    db.refresh(alert)
    return {"data": rows_as_dict([alert])[0]}


@app.patch("/api/alerts/{alert_id}/{action}")
def update_alert(alert_id: int, action: str, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    if action not in {"acknowledge", "resolve"}:
        raise HTTPException(status_code=422, detail="Action must be acknowledge or resolve")
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "acknowledged" if action == "acknowledge" else "resolved"
    if action == "resolve": alert.resolved_at = datetime.now(timezone.utc)
    db.add(AuditLog(profile_id=profile.id, action=f"alert.{action}", details=str(alert_id)))
    db.commit()
    return {"data": rows_as_dict([alert])[0]}


@app.get("/api/weather/current")
async def weather_current(latitude: float | None = Query(None, ge=-90, le=90), longitude: float | None = Query(None, ge=-180, le=180), _profile: Profile = Depends(operational_read_access), db: Session = Depends(get_db)):
    lat = settings.open_meteo_latitude if latitude is None else latitude
    lon = settings.open_meteo_longitude if longitude is None else longitude
    result = await fetch_weather(lat, lon, db)
    return {"data": {"source": result["source"], "is_demo": result["is_demo"], "stale": result.get("stale", False), "notice": result.get("notice"), "location": result["location"], "units": result["units"], "current": result["current"], "fetched_at": result.get("fetched_at")}}


@app.get("/api/weather/forecast")
async def weather_forecast(latitude: float | None = Query(None, ge=-90, le=90), longitude: float | None = Query(None, ge=-180, le=180), hours: int = Query(48, ge=1, le=72), _profile: Profile = Depends(operational_read_access), db: Session = Depends(get_db)):
    lat = settings.open_meteo_latitude if latitude is None else latitude
    lon = settings.open_meteo_longitude if longitude is None else longitude
    result = await fetch_weather(lat, lon, db)
    if result["source"] == "unavailable" or not result.get("hourly"):
        raise HTTPException(status_code=503, detail=result.get("notice", "No live weather forecast is available."))
    return {"data": {**result, "hourly": result.get("hourly", [])[:hours]}}


@app.get("/api/analytics/trends")
def trends(days: int = Query(7, ge=1, le=30), mode: str = Query("live", pattern="^(live|demo|all)$"), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    return {"data": trend_series(db, days, mode)}


@app.get("/api/airspace")
def airspace(_profile: Profile = Depends(operational_read_access)):
    return {"data": [{"region":"North demo sector","status":"nominal","source":"fictional sample","updated_at":datetime.now(timezone.utc).isoformat()}, {"region":"East demo sector","status":"advisory","source":"fictional sample","updated_at":datetime.now(timezone.utc).isoformat()}], "meta":{"data_mode":"demo", "notice":"Illustrative administrative status only; not connected to live airspace systems."}}


@app.get("/api/activity")
def activity(limit: int = Query(10, ge=1, le=50), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)).all()
    return {"data": rows_as_dict(rows)}


def _ml_dataset_summary():
    try:
        return ml_dataset_summary()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Maintenance demo dataset is unavailable or invalid.") from exc


@app.get("/api/ml/status")
def ml_status(_profile: Profile | None = Depends(operational_read_access)):
    return {"data": ml_model_status(), "notice": ML_NOTICE}


@app.get("/api/ml/dataset-summary")
def ml_summary(_profile: Profile | None = Depends(operational_read_access)):
    return {"data": _ml_dataset_summary(), "notice": ML_NOTICE}


@app.get("/api/ml/evaluation")
def ml_evaluation(_profile: Profile | None = Depends(operational_read_access)):
    try:
        return {"data": ml_evaluation_metrics(), "notice": ML_NOTICE}
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Model evaluation is unavailable.") from exc


@app.post("/api/ml/predict")
def ml_demo_prediction(payload: MaintenanceDemoPrediction,
                       _profile: Profile | None = Depends(operational_read_access)):
    try:
        return {"data": ml_predict(payload.model_dump()), "notice": ML_NOTICE}
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Demo prediction is unavailable.") from exc


def _ml_question(question: str | None) -> bool:
    import re
    return bool(re.search(r"\b(?:machine learning|ml analytics|model|prediction|predictions|dataset|"
                          r"class distribution|accuracy|precision|recall|f1|confusion matrix)\b",
                          (question or "").lower()))


def _ml_assistant_response(results: dict) -> dict:
    dataset, status, evaluation = results["dataset"], results["status"], results.get("evaluation")
    facts = [
        {"label": "ML model status", "value": status["status"],
         "basis": "Status read from the backend model artifact and checked against the supplied dataset hash."},
        {"label": "Synthetic dataset rows", "value": str(dataset["rows"]),
         "basis": "Row count loaded from the validated aircraft maintenance demo CSV."},
        {"label": "Class distribution", "value": ", ".join(f"{k}: {v}" for k, v in dataset["class_distribution"].items()),
         "basis": "Backend counts by demo target label in the supplied dataset."},
    ]
    if evaluation:
        facts.extend([
            {"label": "Holdout accuracy", "value": f"{evaluation['accuracy']:.4f}",
             "basis": evaluation["evaluation_basis"]},
            {"label": "Macro precision / recall / F1",
             "value": " / ".join(f"{evaluation['macro_avg'][k]:.4f}" for k in ("precision", "recall", "f1-score")),
             "basis": evaluation["evaluation_basis"]},
        ])
    return {
        "intent": "analysis",
        "summary": ("Verified backend ML results are listed below. " +
                    ("The model has not been trained for this dataset yet." if not status["available"] else
                     "Evaluation values are calculated from the fixed holdout set.")),
        "calculated_data": facts, "interpretation": None, "recommendations": [],
        "limitations": ML_NOTICE + ". The classifier output and evaluation apply only to this synthetic demonstration dataset.",
        "source": "ml-backend", "source_label": "Backend ML analytics",
        "provider": {"name": "Gemini", "configured": bool(settings.gemini_api_key), "used": False},
        "ai_generated": False, "verified": True, "ml_analytics_included": True,
        "ml_notice": ML_NOTICE, "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/ai/analyze")
async def ai_analyze(payload: AnalyzeInput, profile: Profile = Depends(current_user), db: Session = Depends(get_db)):
    if _ml_question(payload.question):
        try:
            return {"data": _ml_assistant_response(ml_assistant_results())}
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Verified ML results are unavailable.") from exc
    intent = classify_analyzer_intent(payload.question)
    if intent != "analysis":
        return {"data": await analyze({}, payload.question)}
    # Ignore caller-provided metrics: analysis always uses the current trusted
    # backend snapshot to prevent stale or fabricated client values.
    snapshot = dashboard_summary(db)
    latest_weather = db.scalar(
        select(WeatherObservation)
        .where(WeatherObservation.source == "open-meteo", WeatherObservation.fetched_at.is_not(None))
        .order_by(WeatherObservation.fetched_at.desc(), WeatherObservation.observed_at.desc())
        .limit(1)
    )
    if latest_weather:
        fetched_at = latest_weather.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        observation_timestamp = latest_weather.observed_at
        if observation_timestamp.tzinfo is None:
            observation_timestamp = observation_timestamp.replace(tzinfo=timezone.utc)
        age_seconds = max(0, (datetime.now(timezone.utc) - fetched_at).total_seconds())
        weather_state = "stale" if age_seconds > settings.weather_cache_seconds else "available"
        snapshot["data_sources"] = {
            "database": {"status": "connected", "mode": snapshot["meta"]["data_mode"]},
            "weather": {
                "status": weather_state,
                "source": "open-meteo",
                "location": latest_weather.location,
                "latitude": latest_weather.latitude,
                "longitude": latest_weather.longitude,
                "observation_timestamp": observation_timestamp.isoformat(),
                "data_type": latest_weather.data_type,
                "last_update": fetched_at.isoformat(),
                "measurements": {
                    "temperature_c": latest_weather.temperature_c,
                    "humidity_pct": latest_weather.humidity_pct,
                    "wind_kmh": latest_weather.wind_kmh,
                    "precipitation_mm": latest_weather.precipitation_mm,
                },
                "freshness_rule": f"stale when last fetched more than {settings.weather_cache_seconds} seconds ago",
            },
        }
    else:
        snapshot["data_sources"] = {
            "database": {"status": "connected", "mode": snapshot["meta"]["data_mode"]},
            "weather": {"status": "unavailable", "source": None,
                        "location": settings.open_meteo_location,
                        "latitude": settings.open_meteo_latitude,
                        "longitude": settings.open_meteo_longitude,
                        "last_update": None,
                        "notice": "No saved Open-Meteo provider record with a fetch timestamp exists."},
        }
    result = await analyze(snapshot, payload.question)
    return {"data": result}


@app.get("/api/reports")
def reports(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), days: int = Query(7), profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    since, _ = selected_date_window(days)
    query = select(AnalysisReport).where(AnalysisReport.created_at >= since).order_by(AnalysisReport.created_at.desc())
    if profile is None:
        query = query.where(AnalysisReport.report_type.in_(("operational_snapshot", "demo_dashboard_snapshot")))
    elif profile.role != "admin":
        query = query.where(or_(AnalysisReport.generated_by == profile.id,
                                AnalysisReport.report_type.in_(("operational_snapshot", "demo_dashboard_snapshot"))))
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    read_scope = "shared snapshots" if profile is None else "own and shared snapshots" if profile.role != "admin" else "all reports"
    return {"data": rows_as_dict(rows, omit=("filters_json",)), "meta": {"limit": limit, "offset": offset, "days": days, "date_field": "created_at", "timezone": "UTC", "read_scope": read_scope}}


@app.post("/api/reports/recalculate")
def recalculate_report(days: int = Query(7), profile: Profile = Depends(current_user), db: Session = Depends(get_db)):
    since, until = selected_date_window(days)
    aircraft_rows = list(db.scalars(select(Aircraft).where(Aircraft.updated_at >= since)).all())
    crew_rows = list(db.scalars(select(Crew).where(Crew.confirmation_at.is_not(None), Crew.confirmation_at >= since)).all())
    maintenance_rows = list(db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.created_at >= since)).all())
    schedule_rows = list(db.scalars(select(ScheduleRecord).where(ScheduleRecord.starts_at >= since, ScheduleRecord.starts_at <= until)).all())
    alert_rows = list(db.scalars(select(Alert).where(Alert.created_at >= since)).all())
    is_demo_snapshot = any(row.is_demo for row in [*aircraft_rows, *crew_rows, *maintenance_rows, *schedule_rows, *alert_rows])
    metrics = calculate_operational_metrics(aircraft_rows, crew_rows)
    done_tasks = sum(row.status in {"complete", "completed"} for row in [*maintenance_rows, *schedule_rows])
    all_tasks = len(maintenance_rows) + len(schedule_rows)
    derived = {
        **metrics,
        "maintenance_records": len(maintenance_rows),
        "schedule_records": len(schedule_rows),
        "task_completion_pct": round(100 * done_tasks / all_tasks) if all_tasks else None,
        "alerts_by_severity": {level: sum(row.severity == level for row in alert_rows) for level in ("low", "medium", "high", "critical")},
        "window_start": since.isoformat(), "window_end": until.isoformat(), "timezone": "UTC",
        "data_origin": "SYNTHETIC_DEMO" if is_demo_snapshot else "DATABASE_RECORDS",
    }
    report = db.scalar(select(AnalysisReport).where(
        AnalysisReport.generated_by == profile.id,
        AnalysisReport.report_type == "operational_snapshot",
        AnalysisReport.is_demo.is_(is_demo_snapshot),
    ).order_by(AnalysisReport.created_at.desc()))
    if report is None:
        report = AnalysisReport(report_type="operational_snapshot", generated_by=profile.id, is_demo=is_demo_snapshot)
        db.add(report)
    report.filters_json = json.dumps({"days": days, "metrics": derived}, default=str)
    report.summary = (f"{days}-day snapshot: {len(aircraft_rows)} aircraft records; "
                      f"{len(crew_rows)} crew records; {len(maintenance_rows)} maintenance items; "
                      f"{len(schedule_rows)} schedule items; {len(alert_rows)} alerts; "
                      f"task completion {derived['task_completion_pct'] if derived['task_completion_pct'] is not None else 'unavailable'}%.")
    report.created_at = datetime.now(timezone.utc)
    db.add(AuditLog(profile_id=profile.id, action="report.recalculated", details=f"days={days}"))
    db.commit()
    db.refresh(report)
    return {"data": rows_as_dict([report])[0], "metrics": derived}


@app.post("/api/reports", status_code=201)
def create_report(payload: ReportCreate, profile: Profile = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    metrics = dashboard_summary(db)
    import json
    readiness = f"{metrics['readiness_pct']}% confirmed readiness" if metrics["readiness_pct"] is not None else "readiness confirmation unavailable"
    report = AnalysisReport(report_type=payload.report_type, filters_json=json.dumps(payload.filters), summary=f"Calculated dashboard snapshot: {metrics['aircraft_total']} aircraft records; {readiness}; {metrics['alerts_open']} open alerts.", generated_by=profile.id)
    db.add(report)
    db.add(AuditLog(profile_id=profile.id, action="report.created", details=payload.report_type))
    db.commit()
    db.refresh(report)
    return {"data": rows_as_dict([report])[0]}


@app.get("/api/admin/roles")
def roles(_profile: Profile = Depends(current_user)):
    return {"data": ["admin", "analyst", "viewer"], "notice": "New self-registered accounts receive viewer access. An administrator must assign elevated roles."}
