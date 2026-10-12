"""Load, validate, import, and aggregate the versioned public inventory snapshot."""
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import PublicInventoryRecord

DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "india_military_aircraft_2026_10_10.json"
ORGANIZATIONS = ("Indian Air Force", "Indian Army Aviation", "Indian Navy Aviation")


def load_dataset(path: Path = DATASET_PATH) -> dict:
    dataset = json.loads(path.read_text(encoding="utf-8"))
    if dataset.get("data_classification") != "ESTIMATE" or not dataset.get("source_url"):
        raise ValueError("Inventory dataset must retain estimate classification and source attribution")
    keys = set()
    for record in dataset.get("records", []):
        if record.get("organization") not in ORGANIZATIONS:
            raise ValueError(f"Unknown organization: {record.get('organization')}")
        if not record.get("display_name") or not record.get("category") or int(record.get("estimated_count", 0)) <= 0:
            raise ValueError("Every inventory row needs a model, category, and positive count")
        key = record_key(dataset["dataset_version"], record)
        if key in keys:
            raise ValueError(f"Duplicate inventory key: {key}")
        keys.add(key)
    return dataset


def record_key(version: str, record: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", record["display_name"].lower()).strip("-")
    org = {"Indian Air Force":"IAF", "Indian Army Aviation":"ARMY", "Indian Navy Aviation":"NAVY"}[record["organization"]]
    suffix = "-order" if record.get("count_type") == "procurement_order" else ""
    return f"IN-{org}-{slug}-{version}{suffix}"[:120]


def import_inventory(db: Session, dataset: dict | None = None) -> dict:
    dataset = dataset or load_dataset()
    imported_at = datetime.now(timezone.utc)
    inserted = updated = 0
    for item in dataset["records"]:
        key = record_key(dataset["dataset_version"], item)
        record = db.scalar(select(PublicInventoryRecord).where(PublicInventoryRecord.record_key == key))
        if record is None:
            record = PublicInventoryRecord(record_key=key)
            db.add(record)
            inserted += 1
        else:
            updated += 1
        count_type = item.get("count_type", "estimated_inventory")
        record.organization = item["organization"]
        record.display_name = item["display_name"]
        record.category = item["category"]
        record.estimated_count = int(item["estimated_count"])
        record.count_type = count_type
        record.country = dataset["country"]
        record.role = item.get("role") or item["category"].replace(" aircraft", "").replace(" fighters", " fighter")
        record.source_name = dataset["source_name"]
        record.source_url = dataset["source_url"]
        record.source_date = datetime.fromisoformat(dataset["source_date"].replace("Z", "+00:00"))
        record.last_imported_at = imported_at
        record.verification_status = dataset["verification_status"]
        record.data_classification = dataset["data_classification"]
        record.is_active_inventory = item.get("is_active_inventory", True)
        record.dataset_version = dataset["dataset_version"]
    db.commit()
    return {"inserted": inserted, "updated": updated, "records": len(dataset["records"]), "last_imported_at": imported_at.isoformat(), "dataset_version": dataset["dataset_version"]}


def inventory_summary(records: list[PublicInventoryRecord], dataset: dict | None = None) -> dict:
    dataset = dataset or load_dataset()
    by_service: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    order_by_service: dict[str, int] = defaultdict(int)
    for row in records:
        if row.count_type == "procurement_order" or not row.is_active_inventory:
            order_by_service[row.organization] += row.estimated_count
        else:
            by_service[row.organization] += row.estimated_count
            by_category[row.category] += row.estimated_count
    calculated = dict(by_service)
    calculated["All services"] = sum(by_service.values())
    discrepancies = []
    for label, expected in dataset["reported_approximate_totals"].items():
        actual = calculated.get(label)
        if actual is not None and actual != expected:
            discrepancies.append({"scope":label,"reported_approximate":expected,"calculated_from_rows":actual,"difference":actual-expected,"note":"Approximate aggregate versus supplied individual row sum; source counts are preserved."})
    return {"service_totals": dict(by_service), "category_totals": dict(by_category), "procurement_by_service": dict(order_by_service), "procurement_total": sum(order_by_service.values()), "inventory_total": sum(by_service.values()), "reported_approximate_totals": dataset["reported_approximate_totals"], "discrepancies": discrepancies}


def filtered_inventory(db: Session, organization: str | None = None, category: str | None = None, model: str | None = None, dataset_version: str | None = None) -> list[PublicInventoryRecord]:
    query = select(PublicInventoryRecord).order_by(PublicInventoryRecord.organization, PublicInventoryRecord.category, PublicInventoryRecord.display_name)
    if dataset_version:
        query = query.where(PublicInventoryRecord.dataset_version == dataset_version)
    if organization:
        query = query.where(PublicInventoryRecord.organization == organization)
    if category:
        query = query.where(PublicInventoryRecord.category == category)
    if model:
        query = query.where(PublicInventoryRecord.display_name.ilike(f"%{model}%"))
    return list(db.scalars(query).all())
