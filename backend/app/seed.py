from datetime import datetime, timedelta, timezone
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from backend.app.models import Aircraft, Crew, MaintenanceRecord, ScheduleRecord, Alert, WeatherObservation, AuditLog, AnalysisReport


def remove_seeded_demo_data(db: Session) -> int:
    """Remove only records carrying the exact identifiers used by seed_demo_data."""
    predicates = (
        (Aircraft, Aircraft.resource_id.like("DEMO-A-%")),
        (Crew, Crew.display_id.like("DEMO-C-%")),
        (MaintenanceRecord, MaintenanceRecord.title.like("Routine inspection · Demo resource %")),
        (ScheduleRecord, ScheduleRecord.title.like("Administrative readiness review __")),
        (Alert, Alert.message.in_((
            "Inspection due · Demo resource A-014",
            "Moderate wind advisory · Demo east sector",
            "Sample crew roster confirmation",
        ))),
        (WeatherObservation, WeatherObservation.source == "demo"),
        (AnalysisReport, AnalysisReport.summary.like("Sample report ·%")),
        (AuditLog, AuditLog.action.in_((
            "demo.readiness_review", "demo.maintenance_scheduled", "demo.weather_advisory",
        ))),
    )
    removed = sum(db.execute(delete(model).where(predicate)).rowcount or 0 for model, predicate in predicates)
    if removed:
        db.commit()
    return removed


def remove_demo_aircraft(db: Session) -> int:
    """Delete only the fictional DEMO-A-* airframes created by earlier sample seeding."""
    removed = db.execute(delete(Aircraft).where(Aircraft.resource_id.like("DEMO-A-%"))).rowcount or 0
    if removed:
        db.commit()
    return removed


def seed_demo_data(db: Session) -> None:
    """Insert fictional demo rows only into an empty table; never wipes existing data."""
    now = datetime.now(timezone.utc)
    # Aircraft are intentionally not seeded: the fleet comes from the supplied inventory dataset.
    if db.scalar(select(Crew.id).limit(1)) is None:
        for i in range(1, 26):
            db.add(Crew(display_id=f"DEMO-C-{i:03d}", group_name=f"Group {(i - 1) // 5 + 1}", readiness_status="ready" if i % 17 else "review", confirmation_at=now - timedelta(hours=i)))
    if db.scalar(select(MaintenanceRecord.id).limit(1)) is None:
        for i in range(1, 13):
            db.add(MaintenanceRecord(aircraft_id=None, title=f"Routine inspection · Demo resource {i:02d}", status="open" if i <= 6 else "in_progress" if i <= 9 else "completed", priority="high" if i in {2, 7} else "normal", due_at=now + timedelta(days=i - 4)))
    if db.scalar(select(ScheduleRecord.id).limit(1)) is None:
        for i in range(18):
            db.add(ScheduleRecord(title=f"Administrative readiness review {i + 1:02d}", category="review", status="scheduled" if i % 4 else "complete", starts_at=now + timedelta(hours=i * 3), ends_at=now + timedelta(hours=i * 3 + 1)))
    if db.scalar(select(Alert.id).limit(1)) is None:
        for severity, category, msg, hours in [("medium", "Maintenance", "Inspection due · Demo resource A-014", 1), ("high", "Weather", "Moderate wind advisory · Demo east sector", 2), ("low", "Readiness", "Sample crew roster confirmation", 5)]:
            db.add(Alert(severity=severity, category=category, message=msg, created_at=now - timedelta(hours=hours)))
    if db.scalar(select(WeatherObservation.id).limit(1)) is None:
        for i in range(24):
            db.add(WeatherObservation(observed_at=now - timedelta(hours=23 - i), location="Demo region", temperature_c=17 + (i % 7) * .6, humidity_pct=58 + i % 14, wind_kmh=10 + i % 9, precipitation_mm=0.2 if i % 8 == 0 else 0, source="demo", data_type="demo", created_at=now))
    if db.scalar(select(AuditLog.id).limit(1)) is None:
        for action, details, hours in [("demo.readiness_review", "Fictional readiness review completed for demo group", 1), ("demo.maintenance_scheduled", "Fictional routine inspection scheduled for demo resource", 2), ("demo.weather_advisory", "Fictional weather advisory updated for demo region", 3)]:
            db.add(AuditLog(action=action, details=details, created_at=now - timedelta(hours=hours)))
    if db.scalar(select(AnalysisReport.id).limit(1)) is None:
        for report_type, summary, hours in [
            ("Fleet readiness", "Sample report · Fictional readiness snapshot across demo aircraft and crew groups.", 4),
            ("Maintenance summary", "Sample report · Fictional summary of open, in-progress and completed demo inspections.", 8),
            ("Weather impact", "Sample report · Fictional weather advisory review for the demo region.", 12),
        ]:
            db.add(AnalysisReport(report_type=report_type, filters_json="{}", summary=summary, generated_by=None, created_at=now - timedelta(hours=hours)))
    db.commit()
