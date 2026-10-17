from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.app.database import Base
from backend.app.models import Aircraft, Crew, MaintenanceRecord, ScheduleRecord, Alert, WeatherObservation, AuditLog
from backend.app.seed import remove_seeded_demo_data, seed_demo_data


def test_cleanup_removes_seed_records_and_preserves_other_rows():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_demo_data(db)
        db.add(Aircraft(resource_id="REAL-001", category="Utility", availability_status="available", maintenance_status="clear"))
        db.commit()

        removed = remove_seeded_demo_data(db)

        assert removed > 0
        assert db.scalar(select(Aircraft).where(Aircraft.resource_id == "REAL-001")) is not None
        for model in (Crew, MaintenanceRecord, ScheduleRecord, Alert, WeatherObservation, AuditLog):
            assert db.scalar(select(model).limit(1)) is None
        assert db.scalar(select(Aircraft).where(Aircraft.resource_id.like("DEMO-A-%")).limit(1)) is None
    engine.dispose()
