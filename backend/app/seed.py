from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.app.models import Aircraft, Crew, MaintenanceRecord, ScheduleRecord, Alert, WeatherObservation, AuditLog


def seed_demo_data(db: Session) -> None:
    """Insert fictional demo rows only into an empty table; never wipes existing data."""
    now = datetime.now(timezone.utc)
    if db.scalar(select(Aircraft.id).limit(1)) is None:
        for i in range(1, 49):
            maintenance = i in {4, 9, 14, 22, 35, 41}
            db.add(Aircraft(resource_id=f"DEMO-A-{i:03d}", category=["Transport", "Training", "Utility"][i % 3], availability_status="maintenance" if maintenance else "available" if i % 5 else "standby", maintenance_status="in_progress" if maintenance else "clear", readiness_confirmation=not maintenance, notes="Fictional sample record"))
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
    db.commit()
