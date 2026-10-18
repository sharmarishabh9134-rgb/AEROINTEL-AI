from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from backend.app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Profile(Base):
    __tablename__ = "profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Aircraft(Base):
    __tablename__ = "aircraft"
    __table_args__ = (Index("uq_aircraft_resource_id_normalized", text("lower(trim(resource_id))"), unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(80))
    availability_status: Mapped[str] = mapped_column(String(30))
    maintenance_status: Mapped[str] = mapped_column(String(30))
    readiness_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    registration: Mapped[str | None] = mapped_column(String(24), nullable=True)
    aircraft_type: Mapped[str | None] = mapped_column(String(140), nullable=True)
    base_airport: Mapped[str | None] = mapped_column(String(4), nullable=True)
    next_maintenance_due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hours_since_check: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)


class PublicInventoryRecord(Base):
    """Versioned open-source inventory estimates; separate from operational aircraft records."""
    __tablename__ = "public_inventory_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    record_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    organization: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(140))
    category: Mapped[str] = mapped_column(String(60), index=True)
    estimated_count: Mapped[int] = mapped_column(Integer)
    count_type: Mapped[str] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(80), default="India")
    role: Mapped[str] = mapped_column(String(120))
    source_name: Mapped[str] = mapped_column(String(160))
    source_url: Mapped[str] = mapped_column(String(500))
    source_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    verification_status: Mapped[str] = mapped_column(String(60), default="unverified_open_source_estimate")
    data_classification: Mapped[str] = mapped_column(String(40), default="ESTIMATE")
    is_active_inventory: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    dataset_version: Mapped[str] = mapped_column(String(40), index=True)


class Crew(Base):
    __tablename__ = "crew"
    __table_args__ = (Index("uq_crew_display_id_normalized", text("lower(trim(display_id))"), unique=True),)
    id: Mapped[int] = mapped_column(primary_key=True)
    display_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    group_name: Mapped[str] = mapped_column(String(80))
    readiness_status: Mapped[str] = mapped_column(String(30))
    confirmation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str | None] = mapped_column(String(60), nullable=True)
    license_rating: Mapped[str | None] = mapped_column(String(120), nullable=True)
    duty_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    rest_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    medical_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    currency_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    aircraft_id: Mapped[int | None] = mapped_column(ForeignKey("aircraft.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)


class WeatherObservation(Base):
    __tablename__ = "weather_observations"
    __table_args__ = (
        Index(
            "uq_weather_source_location_observed_at",
            "source", "latitude", "longitude", "observed_at",
            unique=True,
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    location: Mapped[str] = mapped_column(String(120))
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="demo")
    data_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=True)


class ScheduleRecord(Base):
    __tablename__ = "schedule_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(30), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    aircraft_id: Mapped[int | None] = mapped_column(ForeignKey("aircraft.id", ondelete="SET NULL"), nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(100), nullable=True)
    origin_icao: Mapped[str | None] = mapped_column(String(4), nullable=True)
    destination_icao: Mapped[str | None] = mapped_column(String(4), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    data_origin: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    report_type: Mapped[str] = mapped_column(String(60))
    filters_json: Mapped[str] = mapped_column(Text, default="{}")
    summary: Mapped[str] = mapped_column(Text)
    generated_by: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


Index("ix_schedule_starts_status", ScheduleRecord.starts_at, ScheduleRecord.status)
