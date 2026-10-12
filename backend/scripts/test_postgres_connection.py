"""Verify PostgreSQL connectivity with a rolled-back temporary-table probe."""

import uuid

from sqlalchemy import inspect, text

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models import Base


def main() -> int:
    settings = get_settings()
    if engine.dialect.name != "postgresql" or settings.demo_mode:
        raise SystemExit("Refusing the live PostgreSQL probe: configure a PostgreSQL DATABASE_URL and set DEMO_MODE=false first.")

    inspector = inspect(engine)
    required = set(Base.metadata.tables)
    existing = set(inspector.get_table_names())
    missing = sorted(required - existing)
    if missing:
        raise SystemExit("PostgreSQL is reachable, but required app tables are missing: " + ", ".join(missing))

    for name, table in Base.metadata.tables.items():
        actual = {column["name"] for column in inspector.get_columns(name)}
        drift = sorted(set(table.columns.keys()) - actual)
        if drift:
            raise SystemExit(f"Schema mismatch in {name}; missing columns: {', '.join(drift)}")

    with engine.connect() as connection:
        if connection.execute(text("SELECT 1")).scalar_one() != 1:
            raise SystemExit("PostgreSQL safe read check failed.")
        connection.rollback()
        probe = "aerointel_probe_" + uuid.uuid4().hex[:16]
        quoted_probe = engine.dialect.identifier_preparer.quote(probe)
        transaction = connection.begin()
        try:
            connection.execute(text(f"CREATE TEMP TABLE {quoted_probe} (id integer primary key, marker text not null) ON COMMIT DROP"))
            connection.execute(text(f"INSERT INTO {quoted_probe} (id, marker) VALUES (1, 'roundtrip-ok')"))
            result = connection.execute(text(f"SELECT marker FROM {quoted_probe} WHERE id = 1")).scalar_one()
            if result != "roundtrip-ok":
                raise RuntimeError("Temporary-table write/read-back did not match.")
        finally:
            transaction.rollback()
        remaining = connection.execute(text("SELECT to_regclass(:name)"), {"name": "pg_temp." + probe}).scalar_one_or_none()
        if remaining is not None:
            raise RuntimeError("Temporary probe table remained after rollback.")

    print("PostgreSQL connection, required table/column checks, safe read, and rolled-back temporary write/read-back all passed.")
    print("The probe created no persistent tables or rows and did not touch application records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
