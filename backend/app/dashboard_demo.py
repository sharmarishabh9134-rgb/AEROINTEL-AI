"""Idempotent local-only dashboard fixtures, explicitly marked as synthetic demo data."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models import (
    Aircraft, Alert, AnalysisReport, Crew, MaintenanceRecord, Profile, ScheduleRecord,
)
from backend.app.seed import seed_demo_data
from backend.app.services import calculate_operational_metrics
from backend.app.inventory import load_dataset

AIRPORTS = ("VIDP", "VABB", "VOBL", "VECC", "VAGO", "VAPO")


def seed_dashboard_demo_data(db: Session, *, allow_production: bool = False) -> dict:
    settings = get_settings()
    is_production = settings.app_env.lower() in {"production", "prod"}
    if is_production and not allow_production:
        raise RuntimeError("Production seeding requires the explicit --allow-production option.")
    if engine.dialect.name not in ({"sqlite", "postgresql"} if allow_production else {"sqlite"}):
        raise RuntimeError("Dashboard samples may only be seeded into SQLite or explicitly authorized PostgreSQL.")
    if not is_production and engine.dialect.name == "sqlite" and not settings.demo_mode:
        raise RuntimeError("Set DEMO_MODE=true before seeding a local SQLite demo database.")
    if is_production and engine.dialect.name != "postgresql":
        raise RuntimeError("Production DATABASE_URL must point to PostgreSQL.")

    seed_demo_data(db)
    now = datetime.now(timezone.utc)
    inventory = [row for row in load_dataset()["records"] if row.get("is_active_inventory", True)]
    if not inventory:
        raise RuntimeError("No public aircraft type estimates are available for the labelled demo fixtures.")
    aircraft = list(db.scalars(select(Aircraft).where(
        Aircraft.resource_id.like("DEMO-A-%") | Aircraft.resource_id.like("SAMPLE-A-%")
    ).order_by(Aircraft.resource_id)))
    crew = list(db.scalars(select(Crew).where(
        Crew.display_id.like("DEMO-C-%") | Crew.display_id.like("SAMPLE-C-%")
    ).order_by(Crew.display_id)))
    maintenance = list(db.scalars(select(MaintenanceRecord).where(
        MaintenanceRecord.title.like("Routine inspection · Demo resource %") |
        MaintenanceRecord.title.like("SYNTHETIC DEMO inspection · %")
    ).order_by(MaintenanceRecord.id)))
    schedules = list(db.scalars(select(ScheduleRecord).where(
        ScheduleRecord.title.like("Administrative readiness review %") |
        ScheduleRecord.title.like("DEMO task %")
    ).order_by(ScheduleRecord.id)))
    alerts = list(db.scalars(select(Alert).where(Alert.message.in_((
        "Inspection due · Demo resource A-014", "Moderate wind advisory · Demo east sector", "Sample crew roster confirmation",
    )) | Alert.message.like("SYNTHETIC DEMO · %")).order_by(Alert.id)))
    seen_demo_messages: set[str] = set()
    for row in alerts:
        if row.message.startswith("SYNTHETIC DEMO · "):
            if row.message in seen_demo_messages:
                db.delete(row)
            seen_demo_messages.add(row.message)
    db.flush()
    alerts = list(db.scalars(select(Alert).where(Alert.message.in_((
        "Inspection due · Demo resource A-014", "Moderate wind advisory · Demo east sector", "Sample crew roster confirmation",
    )) | Alert.message.like("SYNTHETIC DEMO · %")).order_by(Alert.id)))

    # Fill missing fixture slots even if real application rows already exist.
    known_aircraft = {row.resource_id for row in aircraft}
    for index in range(1, 49):
        if f"DEMO-A-{index:03d}" not in known_aircraft and f"SAMPLE-A-{index:03d}" not in known_aircraft:
            db.add(Aircraft(resource_id=f"SAMPLE-A-{index:03d}", category="Demo aircraft", availability_status="available",
                            maintenance_status="clear", readiness_confirmation=True, notes="Synthetic demo fixture"))
    known_crew = {row.display_id for row in crew}
    for index in range(1, 26):
        if f"DEMO-C-{index:03d}" not in known_crew and f"SAMPLE-C-{index:03d}" not in known_crew:
            db.add(Crew(display_id=f"SAMPLE-C-{index:03d}", group_name="Demo group", readiness_status="ready"))
    known_maintenance = {row.title for row in maintenance}
    for index in range(1, 13):
        title = f"SYNTHETIC DEMO inspection · SAMPLE-A-{index:03d}"
        if not any(f"Demo resource {index:02d}" in name for name in known_maintenance) and title not in known_maintenance:
            db.add(MaintenanceRecord(title=title, status="open", priority="normal", created_at=now))
    known_schedules = {row.title for row in schedules}
    for index in range(1, 19):
        title = f"DEMO task {index:02d} · {AIRPORTS[(index - 1) % len(AIRPORTS)]} to {AIRPORTS[index % len(AIRPORTS)]}"
        if f"Administrative readiness review {index:02d}" not in known_schedules and title not in known_schedules:
            starts = now + timedelta(days=(index - 1) % 15 - 7)
            db.add(ScheduleRecord(title=title, category="Flight · synthetic demo", status="scheduled",
                                  starts_at=starts, ends_at=starts + timedelta(hours=2)))
    alert_templates = (("high", "overdue maintenance scenario"), ("medium", "sample weather scenario"), ("low", "crew currency scenario"))
    for index in range(len(alerts), len(alert_templates)):
        severity, description = alert_templates[index]
        db.add(Alert(severity=severity, category="Demo scenario", message=f"SYNTHETIC DEMO · {description}", created_at=now))
    db.flush()
    aircraft = list(db.scalars(select(Aircraft).where(
        Aircraft.resource_id.like("DEMO-A-%") | Aircraft.resource_id.like("SAMPLE-A-%")
    ).order_by(Aircraft.resource_id)))
    crew = list(db.scalars(select(Crew).where(
        Crew.display_id.like("DEMO-C-%") | Crew.display_id.like("SAMPLE-C-%")
    ).order_by(Crew.display_id)))
    maintenance = list(db.scalars(select(MaintenanceRecord).where(
        MaintenanceRecord.title.like("Routine inspection · Demo resource %") |
        MaintenanceRecord.title.like("SYNTHETIC DEMO inspection · %")
    ).order_by(MaintenanceRecord.id)))
    schedules = list(db.scalars(select(ScheduleRecord).where(
        ScheduleRecord.title.like("Administrative readiness review %") | ScheduleRecord.title.like("DEMO task %")
    ).order_by(ScheduleRecord.id)))
    alerts = list(db.scalars(select(Alert).where(
        Alert.message.in_(("Inspection due · Demo resource A-014", "Moderate wind advisory · Demo east sector", "Sample crew roster confirmation")) |
        Alert.message.like("SYNTHETIC DEMO · %")
    ).order_by(Alert.id)))

    for index, row in enumerate(aircraft):
        type_row = inventory[index % len(inventory)]
        row.aircraft_type = type_row["display_name"]
        row.category = type_row["category"][:80]
        row.registration = f"DEMO-REG-{index + 1:03d}"
        row.resource_id = f"SAMPLE-A-{index + 1:03d}"
        row.base_airport = AIRPORTS[index % len(AIRPORTS)]
        row.availability_status = ("maintenance" if index % 9 == 0 else "unavailable" if index % 13 == 0
                                   else "standby" if index % 5 == 0 else "available")
        row.maintenance_status = "in_progress" if row.availability_status == "maintenance" else "clear"
        row.readiness_confirmation = row.availability_status == "available"
        row.next_maintenance_due = now + timedelta(days=(index % 17) - 4)
        row.hours_since_check = float(18 + (index * 11) % 260)
        row.updated_at = now - timedelta(hours=index % 120)
        row.is_demo = True
        row.data_origin = "synthetic_demo"
        row.notes = ("SYNTHETIC DEMO ONLY. Aircraft type is drawn from a dated public estimate; "
                     "registration, airport assignment, status, and maintenance values are fictional.")

    db.flush()

    for index, row in enumerate(crew):
        row.display_id = f"SAMPLE-C-{index + 1:03d}"
        row.display_name = f"Fictional Crew Member {index + 1:02d}"
        row.role = ("Pilot" if index % 3 == 0 else "Co-pilot" if index % 3 == 1 else "Flight engineer")
        row.license_rating = "Demo multi-engine instrument rating · fictional"
        row.duty_hours = float(1 + index % 10)
        row.rest_hours = float(8 + (index * 3) % 16)
        row.confirmation_at = now - timedelta(hours=index % 120)
        row.medical_expiry = now + timedelta(days=(index % 15) - 3)
        row.currency_expiry = now + timedelta(days=(index % 20) - 4)
        row.is_demo = True
        row.readiness_status = "review" if index % 8 == 0 else "ready" if row.rest_hours >= 10 else "unavailable"
        row.group_name = f"DEMO group {(index // 5) + 1}"

    crew_names = [row.display_name for row in crew] or ["Fictional Crew Member"]
    for index, row in enumerate(maintenance):
        row.aircraft_id = aircraft[index % len(aircraft)].id if aircraft else None
        row.status = "overdue" if index % 5 == 0 else "in_progress" if index % 3 == 0 else "open" if index % 3 == 1 else "completed"
        row.priority = "high" if row.status == "overdue" else "normal"
        row.created_at = now - timedelta(days=index % 7, hours=index % 12)
        row.due_at = now + timedelta(days=(index % 9) - 3)
        row.is_demo = True
        row.data_origin = "synthetic_demo"
        row.title = f"SYNTHETIC DEMO inspection · {aircraft[index % len(aircraft)].resource_id if aircraft else 'unassigned'}"

    for index, row in enumerate(schedules):
        offset = index % 15 - 7
        row.starts_at = now + timedelta(days=offset, hours=index % 8)
        row.ends_at = row.starts_at + timedelta(hours=2)
        row.aircraft_id = aircraft[index % len(aircraft)].id if aircraft else None
        row.assignee = crew_names[index % len(crew_names)]
        row.priority = ("high" if index % 7 == 0 else "normal")
        row.origin_icao = AIRPORTS[index % len(AIRPORTS)]
        row.destination_icao = AIRPORTS[(index + 1) % len(AIRPORTS)]
        row.category = "Flight · synthetic demo" if index % 2 == 0 else "Maintenance · synthetic demo"
        row.status = "complete" if offset < 0 and index % 3 == 0 else "in_progress" if offset <= 0 else "scheduled"
        row.is_demo = True
        row.data_origin = "synthetic_demo"
        row.title = f"DEMO task {index + 1:02d} · {row.origin_icao} to {row.destination_icao}"

    for index, row in enumerate(alerts):
        row.is_demo = True
        row.data_origin = "synthetic_demo"
        row.is_read = bool(index == 2)
        row.created_at = now - timedelta(hours=index + 1)
        if index == 0:
            row.message = "SYNTHETIC DEMO · overdue maintenance scenario · not an actual aircraft report"
            row.category = "Demo maintenance"
            row.severity = "high"
        elif index == 1:
            row.message = "SYNTHETIC DEMO · sample weather advisory · not a live METAR alert"
            row.category = "Demo weather"
            row.severity = "medium"
        else:
            row.message = "SYNTHETIC DEMO · sample crew currency reminder · not a real person"
            row.category = "Demo crew currency"
            row.severity = "low"

    db.flush()
    metrics = calculate_operational_metrics(aircraft, crew)
    maintenance_done = sum(row.status == "completed" for row in maintenance)
    schedule_done = sum(row.status == "complete" for row in schedules)
    all_tasks = len(maintenance) + len(schedules)
    snapshot = {
        **metrics,
        "maintenance_records": len(maintenance), "schedule_records": len(schedules),
        "task_completion_pct": round(100 * (maintenance_done + schedule_done) / all_tasks) if all_tasks else None,
        "alerts_by_severity": {level: sum(row.severity == level for row in alerts) for level in ("low", "medium", "high", "critical")},
        "data_origin": "SYNTHETIC_DEMO", "generated_at": now.isoformat(),
    }
    profiles = list(db.scalars(select(Profile)))
    for profile in profiles or [None]:
        report = db.scalar(select(AnalysisReport).where(
            AnalysisReport.report_type == "demo_dashboard_snapshot",
            AnalysisReport.generated_by == (profile.id if profile else None),
            AnalysisReport.is_demo.is_(True),
        ))
        if report is None:
            report = AnalysisReport(report_type="demo_dashboard_snapshot", generated_by=profile.id if profile else None)
            db.add(report)
        report.summary = (f"Synthetic demo snapshot: {len(aircraft)} aircraft, {len(crew)} crew, "
                          f"{len(maintenance)} maintenance items, {len(schedules)} schedule items, "
                          f"{len(alerts)} alerts. Not operational data.")
        report.filters_json = json.dumps(snapshot)
        report.created_at = now
        report.is_demo = True
    db.commit()
    return {"aircraft": len(aircraft), "crew": len(crew), "maintenance": len(maintenance),
            "schedules": len(schedules), "alerts": len(alerts), "reports": max(1, len(profiles)),
            "data_origin": "SYNTHETIC_DEMO"}
