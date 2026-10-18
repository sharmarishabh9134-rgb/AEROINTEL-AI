"""Validated, transaction-safe imports into the existing operational tables."""
from __future__ import annotations

import csv
import io
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import posixpath
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session

from backend.app.models import Aircraft, Alert, AuditLog, MaintenanceRecord, ScheduleRecord
from backend.app.schemas import AircraftInput, MaintenanceInput, ScheduleInput, AlertCreate

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
MAX_PREVIEW_ROWS = 12

SCHEMAS = {
    "aircraft": {
        "required": ["resource_id", "category", "availability_status", "maintenance_status"],
        "fields": ["resource_id", "category", "availability_status", "maintenance_status", "registration", "aircraft_type", "base_airport", "readiness_confirmation", "notes", "next_maintenance_due", "hours_since_check"],
    },
    "maintenance": {
        "required": ["title", "status"],
        "fields": ["aircraft_resource_id", "title", "status", "priority", "due_at", "created_at"],
    },
    "flights": {
        "required": ["title", "category", "starts_at"],
        "fields": ["title", "category", "status", "starts_at", "ends_at", "aircraft_resource_id", "priority", "assignee", "origin_icao", "destination_icao"],
    },
    "alerts": {
        "required": ["severity", "category", "message"],
        "fields": ["severity", "category", "message", "status", "created_at", "resolved_at", "is_read"],
    },
}

ALIASES = {"flights_schedules": "flights", "schedules": "flights"}


def canonical_type(value: str) -> str:
    value = ALIASES.get((value or "").strip().lower(), (value or "").strip().lower())
    if value not in SCHEMAS:
        raise ValueError("Dataset type must be Aircraft, Maintenance, Flights/Schedules, or Alerts.")
    return value


def _read_grid(filename: str, content: bytes) -> tuple[list[str], list[list[Any]]]:
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("File is empty or exceeds the 5 MB upload limit.")
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix == "csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV must use UTF-8 encoding.") from exc
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if not rows:
            raise ValueError("The file has no header row.")
        headers = [str(value).strip() for value in rows[0]]
        return headers, rows[1:]
    if suffix != "xlsx":
        raise ValueError("Only .csv and .xlsx files are supported. Legacy .xls files are not accepted.")
    if not content.startswith(b"PK") or not zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError("The .xlsx file is invalid or corrupted.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if any(name.lower().endswith("vbaproject.bin") for name in names):
                raise ValueError("Macro-enabled workbooks are not accepted.")
            if len(names) > 200 or sum(info.file_size for info in archive.infolist()) > 30 * 1024 * 1024:
                raise ValueError("Workbook expands beyond the safe processing limit.")
            shared = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")) for item in root]
            date_styles = set()
            if "xl/styles.xml" in names:
                style_root = ET.fromstring(archive.read("xl/styles.xml"))
                custom_formats = {int(node.attrib["numFmtId"]): node.attrib.get("formatCode", "")
                                  for node in style_root.iter() if node.tag.endswith("}numFmt")}
                xfs = next((node for node in style_root if node.tag.endswith("}cellXfs")), None)
                if xfs is not None:
                    for index, xf in enumerate(xfs):
                        fmt_id = int(xf.attrib.get("numFmtId", "0"))
                        fmt_code = re.sub(r'"[^"]*"|\\[[^]]+\\]|\\\\.', "", custom_formats.get(fmt_id, "")).lower()
                        if fmt_id in set(range(14, 23)) | set(range(45, 48)) | set(range(27, 37)) | set(range(50, 59)) or any(char in fmt_code for char in ("y", "d")) and "m" in fmt_code:
                            date_styles.add(index)
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            first_sheet = next(node for node in workbook.iter() if node.tag.endswith("}sheet"))
            rel_id = next(value for key, value in first_sheet.attrib.items() if key.endswith("}id"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            target = next(node.attrib["Target"] for node in rels if node.attrib.get("Id") == rel_id)
            sheet_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheet_path = str(posixpath.normpath(sheet_path))
            if not sheet_path.startswith("xl/worksheets/") or sheet_path not in names:
                raise ValueError("Workbook worksheet reference is invalid.")
            root = ET.fromstring(archive.read(sheet_path))
            raw_rows = []
            for row_node in (node for node in root.iter() if node.tag.endswith("}row")):
                cells = []
                for cell in (node for node in row_node if node.tag.endswith("}c")):
                    ref = cell.attrib.get("r", "")
                    col_letters = re.match(r"[A-Z]+", ref)
                    if not col_letters:
                        continue
                    col_index = 0
                    for char in col_letters.group():
                        col_index = col_index * 26 + ord(char) - 64
                    while len(cells) < col_index:
                        cells.append(None)
                    if cell.attrib.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
                    else:
                        v_node = next((node for node in cell if node.tag.endswith("}v")), None)
                        value = v_node.text if v_node is not None else None
                        if cell.attrib.get("t") == "s" and value is not None:
                            value = shared[int(value)]
                        if cell.attrib.get("t") == "b" and value is not None:
                            value = value == "1"
                        if value is not None and cell.attrib.get("s") is not None and int(cell.attrib["s"]) in date_styles:
                            value = (datetime(1899, 12, 30) + timedelta(days=float(value))).replace(tzinfo=timezone.utc).isoformat()
                    if any(node.tag.endswith("}f") for node in cell):
                        raise ValueError(f"Formula cells are not accepted ({ref}).")
                    cells[col_index - 1] = value
                raw_rows.append(cells)
            if not raw_rows or not raw_rows[0]:
                raise ValueError("The file has no header row.")
            return [str(value or "").strip() for value in raw_rows[0]], raw_rows[1:]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The .xlsx file could not be read. Check that it is a valid workbook.") from exc


def _normalize_headers(headers: list[str]) -> list[str]:
    if not headers or any(not item for item in headers):
        raise ValueError("Every column must have a non-empty header.")
    normalized = [re.sub(r"[^a-z0-9]+", "_", item.strip().lower()).strip("_") for item in headers]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Column names must be unique after normalization.")
    return normalized


def _coerce(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _date(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be an ISO-8601 date/time with timezone, e.g. 2026-10-18T12:00:00Z.")


def _boolean(value: Any, field: str) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"{field} must be true/false, yes/no, or 1/0.")


def _float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a finite number.")


def _map_row(row: dict[str, Any], dataset_type: str, mapping: dict[str, str]) -> dict[str, Any]:
    mapped = {target: _coerce(row.get(source)) for target, source in mapping.items() if source in row}
    for field in ("due_at", "created_at", "starts_at", "ends_at", "resolved_at", "next_maintenance_due"):
        if field in mapped:
            mapped[field] = _date(mapped[field], field)
    for field in ("readiness_confirmation", "is_read"):
        if field in mapped:
            mapped[field] = _boolean(mapped[field], field)
    if "hours_since_check" in mapped:
        mapped["hours_since_check"] = _float(mapped["hours_since_check"], "hours_since_check")
    return mapped


def _validate_row(row: dict[str, Any], dataset_type: str) -> dict[str, Any]:
    if dataset_type == "aircraft":
        payload = AircraftInput(**{k: v for k, v in row.items() if v is not None}, is_demo=False)
        return payload.model_dump()
    if dataset_type == "maintenance":
        allowed = {k: v for k, v in row.items() if k in {"title", "status", "priority", "due_at"} and v is not None}
        payload = MaintenanceInput(**allowed, is_demo=False)
        return {**payload.model_dump(), "aircraft_resource_id": row.get("aircraft_resource_id"), "created_at": _date(row.get("created_at"), "created_at") if row.get("created_at") else None}
    if dataset_type == "flights":
        allowed = {k: v for k, v in row.items() if k in {"title", "category", "status", "starts_at", "ends_at", "priority", "assignee", "origin_icao", "destination_icao"} and v is not None}
        payload = ScheduleInput(**allowed, is_demo=False)
        data = payload.model_dump()
        if data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            raise ValueError("ends_at must be after starts_at.")
        return {**data, "aircraft_resource_id": row.get("aircraft_resource_id")}
    allowed = {k: v for k, v in row.items() if k in {"severity", "category", "message"}}
    payload = AlertCreate(**allowed)
    status = row.get("status") or "open"
    if status not in {"open", "acknowledged", "resolved"}:
        raise ValueError("status must be open, acknowledged, or resolved.")
    return {**payload.model_dump(), "status": status, "created_at": _date(row.get("created_at"), "created_at"),
            "resolved_at": _date(row.get("resolved_at"), "resolved_at"), "is_read": _boolean(row.get("is_read"), "is_read") or False}


def _duplicate_key(dataset_type: str, row: dict[str, Any]) -> tuple:
    if dataset_type == "aircraft":
        return (str(row["resource_id"]).strip().casefold(),)
    if dataset_type == "maintenance":
        return (str(row.get("aircraft_resource_id") or "").strip().casefold(), str(row["title"]).strip().casefold(), _date_key(row.get("due_at")))
    if dataset_type == "flights":
        return (str(row["title"]).strip().casefold(), _date_key(row["starts_at"]), row.get("origin_icao"), row.get("destination_icao"))
    return (row["severity"], str(row["category"]).strip().casefold(), str(row["message"]).strip().casefold())


def _date_key(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _existing_keys(db: Session, dataset_type: str) -> set[tuple]:
    if dataset_type == "aircraft":
        return {(row.strip().casefold(),) for row in db.scalars(select(Aircraft.resource_id)).all()}
    if dataset_type == "maintenance":
        rows = db.execute(select(MaintenanceRecord.title, MaintenanceRecord.due_at, Aircraft.resource_id).outerjoin(Aircraft, MaintenanceRecord.aircraft_id == Aircraft.id)).all()
        return {(str(aircraft or "").strip().casefold(), str(title).strip().casefold(), _date_key(due)) for title, due, aircraft in rows}
    if dataset_type == "flights":
        rows = db.execute(select(ScheduleRecord.title, ScheduleRecord.starts_at, ScheduleRecord.origin_icao, ScheduleRecord.destination_icao)).all()
        return {(str(title).strip().casefold(), _date_key(starts), origin, destination) for title, starts, origin, destination in rows}
    rows = db.execute(select(Alert.severity, Alert.category, Alert.message)).all()
    return {(severity, str(category).strip().casefold(), str(message).strip().casefold()) for severity, category, message in rows}


def inspect_upload(filename: str, content: bytes, dataset_type: str, mapping: dict[str, str] | None = None, db: Session | None = None) -> dict[str, Any]:
    dataset_type = canonical_type(dataset_type)
    headers, source_rows = _read_grid(filename, content)
    source_headers = _normalize_headers(headers)
    if len(source_rows) > MAX_ROWS:
        raise ValueError(f"Upload contains {len(source_rows)} data rows; the limit is {MAX_ROWS}.")
    fields = SCHEMAS[dataset_type]["fields"]
    target_mapping = mapping or {field: field for field in fields if field in source_headers}
    if any(target not in fields for target in target_mapping) or any(source not in source_headers for source in target_mapping.values()):
        raise ValueError("Column mapping contains an unknown dataset field or source column.")
    missing = [field for field in SCHEMAS[dataset_type]["required"] if field not in target_mapping]

    table_rows = []
    errors = []
    if missing:
        errors.extend({"row": 1, "message": f"Missing required column mapping: {field}."} for field in missing)
    seen = set()
    existing = _existing_keys(db, dataset_type) if db is not None else set()
    known_aircraft = ({value.strip().casefold() for value in db.scalars(select(Aircraft.resource_id)).all()}
                      if db is not None and dataset_type in {"maintenance", "flights"} else set())
    duplicates = 0
    for index, values in enumerate(source_rows, start=2):
        if len(values) > len(source_headers):
            errors.append({"row": index, "message": "Row has more values than there are columns in the header."})
            continue
        values = values + [None] * max(0, len(source_headers) - len(values))
        source = {header: values[position] for position, header in enumerate(source_headers)}
        if not any(_coerce(value) is not None for value in values):
            continue
        try:
            mapped = _map_row(source, dataset_type, target_mapping)
            validated = _validate_row(mapped, dataset_type)
            aircraft_ref = validated.get("aircraft_resource_id")
            if aircraft_ref and aircraft_ref.strip().casefold() not in known_aircraft:
                raise ValueError(f"Unknown aircraft resource_id: {aircraft_ref}.")
            key = _duplicate_key(dataset_type, validated)
            if key in seen:
                raise ValueError("Duplicate identifier/natural key within this file.")
            seen.add(key)
            table_rows.append({"row_number": index, "values": validated,
                               "will_skip": key in existing, "warning": "Matching record already exists and will be skipped." if key in existing else None})
            duplicates += key in existing
        except (ValueError, ValidationError, TypeError) as exc:
            detail = str(exc)
            if isinstance(exc, ValidationError):
                detail = "; ".join(f"{'.'.join(str(x) for x in item['loc'])}: {item['msg']}" for item in exc.errors())
            errors.append({"row": index, "message": detail})

    return {"dataset_type": dataset_type, "headers": source_headers, "mapping": target_mapping,
            "row_count": len([r for r in source_rows if any(_coerce(v) is not None for v in r)]),
            "valid_rows": len(table_rows), "will_import": len(table_rows) - duplicates, "will_skip": duplicates,
            "errors": errors[:100], "error_count": len(errors),
            "preview": [{"row": r["row_number"], **r["values"], "will_skip": r["will_skip"]} for r in table_rows[:MAX_PREVIEW_ROWS]],
            "ml_compatibility": _ml_compatibility(dataset_type, target_mapping, source_headers),
            "_rows": table_rows}


def _ml_compatibility(dataset_type: str, mapping: dict[str, str], headers: list[str]) -> dict[str, Any]:
    from backend.app.ml_analytics import FEATURES, TARGET
    return {"inference_compatible": False, "training_candidate": False,
            "model_features": FEATURES, "expected_label": TARGET, "label_present": TARGET in headers,
            "reason": "The existing Maintenance table stores work orders, not the model's feature rows. Uploaded operational records are not used for inference or training. The model remains based on its checked synthetic demo dataset.",
            "notice": "SYNTHETIC DEMO DATA — NOT FOR OPERATIONAL USE"}


def commit_rows(db: Session, profile_id: int, inspected: dict[str, Any]) -> dict[str, Any]:
    if inspected["error_count"]:
        raise ValueError("Correct all validation errors before importing.")
    dataset_type = inspected["dataset_type"]
    rows = inspected.pop("_rows", None) or []
    # The endpoint attaches the validated rows only in memory, never to audit history.
    if not rows:
        raise ValueError("Import preview is no longer available. Preview the file again.")
    inserted = skipped = 0
    try:
        with db.begin_nested():
            if db.bind and db.bind.dialect.name == "postgresql":
                # Serialize import commits and refresh natural-key matches after
                # the lock so concurrent uploads cannot duplicate one another.
                db.execute(text("SELECT pg_advisory_xact_lock(741928104)"))
            existing = _existing_keys(db, dataset_type)
            for item in rows:
                values = item["values"]
                key = _duplicate_key(dataset_type, values)
                if key in existing:
                    skipped += 1
                    continue
                if dataset_type == "aircraft":
                    record = Aircraft(**values, data_origin="upload")
                elif dataset_type == "maintenance":
                    aircraft_id = None
                    if values.get("aircraft_resource_id"):
                        aircraft = db.scalar(select(Aircraft).where(func.lower(func.trim(Aircraft.resource_id)) == values["aircraft_resource_id"].strip().lower()))
                        if not aircraft:
                            raise ValueError(f"Maintenance references unknown aircraft {values['aircraft_resource_id']}.")
                        aircraft_id = aircraft.id
                    data = {k: v for k, v in values.items() if k not in {"aircraft_resource_id", "created_at", "aircraft_id", "is_demo"}}
                    record = MaintenanceRecord(**data, aircraft_id=aircraft_id, is_demo=False, data_origin="upload",
                                               created_at=values.get("created_at") or datetime.now(timezone.utc))
                elif dataset_type == "flights":
                    aircraft_id = None
                    if values.get("aircraft_resource_id"):
                        aircraft = db.scalar(select(Aircraft).where(func.lower(func.trim(Aircraft.resource_id)) == values["aircraft_resource_id"].strip().lower()))
                        if not aircraft:
                            raise ValueError(f"Schedule references unknown aircraft {values['aircraft_resource_id']}.")
                        aircraft_id = aircraft.id
                    data = {k: v for k, v in values.items() if k not in {"aircraft_resource_id", "aircraft_id", "is_demo"}}
                    record = ScheduleRecord(**data, aircraft_id=aircraft_id, is_demo=False, data_origin="upload")
                else:
                    record = Alert(**values, is_demo=False, data_origin="upload")
                db.add(record)
                db.flush()
                existing.add(key)
                inserted += 1
        db.add(AuditLog(profile_id=profile_id, action="data_import.completed",
                        details=json.dumps({"dataset_type": dataset_type, "inserted": inserted, "skipped": skipped}, separators=(",", ":"))))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"dataset_type": dataset_type, "inserted": inserted, "skipped": skipped, "failed": 0,
            "policy": "Insert new records; skip matches by business identifier/natural key. No updates are performed."}


def import_history(db: Session, limit: int = 20) -> list[dict[str, Any]]:
    events = db.scalars(select(AuditLog).where(AuditLog.action == "data_import.completed").order_by(AuditLog.created_at.desc()).limit(limit)).all()
    result = []
    for event in events:
        try:
            details = json.loads(event.details)
        except (ValueError, TypeError):
            details = {}
        result.append({"id": event.id, "created_at": event.created_at.isoformat(), **details})
    return result
