"""Versioned official public procurement disclosures, kept separate from inventory."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "india_official_aviation_disclosures_2026_10_18.json"
SERVICES = {"Indian Air Force", "Indian Army Aviation", "Indian Navy Aviation"}


def load_official_disclosures() -> dict:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    if dataset.get("data_origin") != "REAL_PUBLIC_DISCLOSURE":
        raise ValueError("Official disclosure dataset must retain its public-disclosure origin label")
    version = dataset.get("dataset_version")
    imported_at = dataset.get("imported_at")
    records = dataset.get("records")
    if not isinstance(version, str) or not isinstance(imported_at, str) or not isinstance(records, list):
        raise ValueError("Official disclosure dataset is missing version, import timestamp, or records")
    seen: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("Every official disclosure must be an object")
        record_id = row.get("record_id")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise ValueError("Official disclosure record IDs must be present and unique")
        seen.add(record_id)
        if row.get("service") not in SERVICES:
            raise ValueError(f"Unknown service on official disclosure {record_id}")
        if not isinstance(row.get("quantity"), int) or row["quantity"] <= 0:
            raise ValueError(f"Official disclosure {record_id} needs a positive integer quantity")
        date.fromisoformat(row["event_date"])
        if not row.get("source") or not row.get("source_url") or not row.get("event_type"):
            raise ValueError(f"Official disclosure {record_id} is missing source or event attribution")
    return dataset


def filtered_official_disclosures(dataset: dict, service: str | None = None, q: str | None = None) -> list[dict]:
    rows = dataset["records"]
    if service:
        rows = [row for row in rows if row["service"] == service]
    if q:
        needle = q.casefold()
        rows = [row for row in rows if needle in " ".join(str(value) for value in row.values()).casefold()]
    return rows
