"""Idempotently import the bundled, user-supplied public-estimate snapshot."""
import json
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from app.database import Base, engine
from app.inventory import import_inventory, inventory_summary, load_dataset
from app.models import PublicInventoryRecord


def main() -> None:
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(bind=engine, tables=[PublicInventoryRecord.__table__])
    elif "public_inventory_records" not in inspect(engine).get_table_names():
        raise RuntimeError("Apply reviewed migration 003_public_inventory_estimates.sql before importing into PostgreSQL.")
    dataset = load_dataset()
    with Session(engine) as db:
        result = import_inventory(db, dataset)
        rows = db.query(PublicInventoryRecord).filter_by(dataset_version=dataset["dataset_version"]).all()
        report = inventory_summary(rows, dataset)
    print(json.dumps({**result, "summary": report}, indent=2))


if __name__ == "__main__":
    main()
