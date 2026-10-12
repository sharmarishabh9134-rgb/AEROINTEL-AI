import logging
import time
from contextlib import asynccontextmanager
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import Response
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import Base, engine, get_db
from app.models import (Aircraft, Alert, AnalysisReport, AuditLog, Crew, MaintenanceRecord, Profile, PublicInventoryRecord, ScheduleRecord, WeatherObservation)
from app.schemas import (AlertCreate, AircraftInput, AircraftUpdate, AnalyzeInput, CrewInput, CrewUpdate, LoginInput, MaintenanceInput, MaintenanceUpdate, ReportCreate, ScheduleInput, ScheduleUpdate, UserCreate, UserOut)
from app.security import bearer, create_access_token, current_user, get_jwt_secret, password_hash, require_roles
from app.seed import seed_demo_data
from app.services import analyze, classify_analyzer_intent, dashboard_summary, fetch_weather, rows_as_dict, trend_series
from app.inventory import filtered_inventory, inventory_summary, load_dataset

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


def initialize_database():
    is_production = settings.app_env.lower() in {"production", "prod"}
    get_jwt_secret()
    if settings.demo_mode and engine.dialect.name != "sqlite":
        raise RuntimeError("DEMO_MODE may only be used with local SQLite; it must not seed a PostgreSQL database.")
    if is_production and engine.dialect.name != "postgresql":
        raise RuntimeError("Production DATABASE_URL must point to PostgreSQL; SQLite is supported only for local development and tests.")
    if engine.dialect.name == "sqlite" and not is_production:
        Base.metadata.create_all(bind=engine)
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
    if settings.demo_mode and not is_production:
        with Session(engine) as db:
            seed_demo_data(db)


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
    # Public operational reads exist only for the explicitly fictional local demo.
    if settings.demo_mode and credentials is None:
        return None
    return current_user(credentials, db)


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request: Request, exc: SQLAlchemyError):
    logger.error("Database request failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": "Database is temporarily unavailable."})


@app.get("/", include_in_schema=False)
def home():
    path = Path(__file__).resolve().parents[1] / "index.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Dashboard file not found")
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
    return {"data": dashboard_summary(db)}


@app.get("/api/public-inventory")
def public_inventory(
    organization: str | None = Query(None, max_length=80),
    category: str | None = Query(None, max_length=60),
    model: str | None = Query(None, max_length=100),
    db: Session = Depends(get_db),
):
    dataset = load_dataset()
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


@app.get("/api/aircraft")
def aircraft(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(Aircraft).order_by(Aircraft.resource_id).offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset, "data_mode": "demo" if settings.demo_mode else "database"}}


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
def crew(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(Crew).order_by(Crew.display_id).offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset}}


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
def maintenance(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(MaintenanceRecord).order_by(MaintenanceRecord.created_at.desc()).offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset}}


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
def schedules(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(ScheduleRecord).order_by(ScheduleRecord.starts_at).offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset}}


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
def alerts(status: str | None = Query(None, pattern="^(open|acknowledged|resolved)$"), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    query = select(Alert).order_by(Alert.created_at.desc())
    if status:
        query = query.where(Alert.status == status)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows), "meta": {"limit": limit, "offset": offset}}


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
async def weather_current(latitude: float | None = Query(None, ge=-90, le=90), longitude: float | None = Query(None, ge=-180, le=180), db: Session = Depends(get_db)):
    lat = settings.open_meteo_latitude if latitude is None else latitude
    lon = settings.open_meteo_longitude if longitude is None else longitude
    result = await fetch_weather(lat, lon, db)
    return {"data": {"source": result["source"], "is_demo": result["is_demo"], "stale": result.get("stale", False), "notice": result.get("notice"), "location": result["location"], "units": result["units"], "current": result["current"], "fetched_at": result.get("fetched_at")}}


@app.get("/api/weather/forecast")
async def weather_forecast(latitude: float | None = Query(None, ge=-90, le=90), longitude: float | None = Query(None, ge=-180, le=180), hours: int = Query(48, ge=1, le=72), db: Session = Depends(get_db)):
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
def airspace():
    return {"data": [{"region":"North demo sector","status":"nominal","source":"fictional sample","updated_at":datetime.now(timezone.utc).isoformat()}, {"region":"East demo sector","status":"advisory","source":"fictional sample","updated_at":datetime.now(timezone.utc).isoformat()}], "meta":{"data_mode":"demo", "notice":"Illustrative administrative status only; not connected to live airspace systems."}}


@app.get("/api/activity")
def activity(limit: int = Query(10, ge=1, le=50), _profile: Profile | None = Depends(operational_read_access), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)).all()
    return {"data": rows_as_dict(rows)}


@app.post("/api/ai/analyze")
async def ai_analyze(payload: AnalyzeInput, profile: Profile = Depends(current_user), db: Session = Depends(get_db)):
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
def reports(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), profile: Profile = Depends(current_user), db: Session = Depends(get_db)):
    query = select(AnalysisReport).order_by(AnalysisReport.created_at.desc())
    if profile.role != "admin":
        query = query.where(AnalysisReport.generated_by == profile.id)
    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return {"data": rows_as_dict(rows, omit=("filters_json",)), "meta": {"limit": limit, "offset": offset}}


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
def roles():
    return {"data": ["admin", "analyst", "viewer"], "notice": "New self-registered accounts receive viewer access. An administrator must assign elevated roles."}
