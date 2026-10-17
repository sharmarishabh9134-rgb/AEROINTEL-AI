"""Seed synthetic dashboard examples into an explicit SQLite or authorized PostgreSQL database."""
import argparse

from sqlalchemy import inspect

from backend.app.config import get_settings
from backend.app.database import Base, SessionLocal, engine
from backend.app.main import migrate_dashboard_demo_fields, migrate_weather_provenance
from backend.app.dashboard_demo import seed_dashboard_demo_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-production", action="store_true",
                        help="explicitly authorize synthetic demo rows in the configured production PostgreSQL database")
    args = parser.parse_args()
    settings = get_settings()
    is_production = settings.app_env.lower() in {"production", "prod"}
    if is_production:
        if not args.allow_production:
            raise SystemExit("Refusing production seeding without --allow-production. No database was changed.")
        if engine.dialect.name != "postgresql":
            raise SystemExit("Production DATABASE_URL must use PostgreSQL. No database was changed.")
        schema = inspect(engine)
        expected = set(Base.metadata.tables)
        missing_tables = expected - set(schema.get_table_names())
        missing_columns = {
            table: sorted(set(model.columns.keys()) - {column["name"] for column in schema.get_columns(table)})
            for table, model in Base.metadata.tables.items() if table in schema.get_table_names()
        }
        missing_columns = {table: columns for table, columns in missing_columns.items() if columns}
        if missing_tables or missing_columns:
            raise SystemExit("Production schema is not ready; apply reviewed migrations first. No seed rows were written.")
    else:
        if engine.dialect.name != "sqlite" or not settings.demo_mode:
            raise SystemExit("Set a local SQLite DATABASE_URL and DEMO_MODE=true. No database was changed.")
        Base.metadata.create_all(bind=engine)
        migrate_dashboard_demo_fields()
        migrate_weather_provenance()
    with SessionLocal() as db:
        result = seed_dashboard_demo_data(db, allow_production=args.allow_production)
    for name, count in result.items():
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()
