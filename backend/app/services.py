import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.app.config import get_settings
from backend.app.models import Aircraft, Alert, Crew, MaintenanceRecord, ScheduleRecord, WeatherObservation

logger = logging.getLogger("aerointel")
LIVE_WEATHER_SOURCES = ("open-meteo", "met-no")
_weather_failures: dict = {}
_weather_cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}


AIRCRAFT_AVAILABILITY_STATES = {"available", "maintenance", "standby", "unavailable"}
CREW_READINESS_STATES = {"ready", "review", "unavailable"}
ACTIVE_MAINTENANCE_STATES = {"open", "in_progress", "overdue"}


def _percent(numerator: int, denominator: int) -> int | None:
    """Round a non-negative percentage to the nearest whole number, ties up."""
    if denominator <= 0:
        return None
    return (numerator * 100 + denominator // 2) // denominator


def _latest_unique_records(records, identity_field: str, freshness_field: str) -> tuple[list, int, int]:
    """Deduplicate by normalized business ID, retaining the latest known row."""
    unique = {}
    missing_identity_count = 0
    duplicate_count = 0
    for ordinal, record in enumerate(records):
        raw_identity = getattr(record, identity_field, None)
        if isinstance(raw_identity, str) and raw_identity.strip():
            identity = raw_identity.strip().casefold()
        else:
            missing_identity_count += 1
            identity = f"__missing_identity__:{getattr(record, 'id', None) or ordinal}"
        prior = unique.get(identity)
        if prior is None:
            unique[identity] = record
            continue
        duplicate_count += 1
        current_time = getattr(record, freshness_field, None)
        prior_time = getattr(prior, freshness_field, None)
        if isinstance(current_time, datetime) and current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if isinstance(prior_time, datetime) and prior_time.tzinfo is None:
            prior_time = prior_time.replace(tzinfo=timezone.utc)
        current_rank = (current_time.timestamp() if isinstance(current_time, datetime) else float("-inf"), getattr(record, "id", 0) or 0)
        prior_rank = (prior_time.timestamp() if isinstance(prior_time, datetime) else float("-inf"), getattr(prior, "id", 0) or 0)
        if current_rank > prior_rank:
            unique[identity] = record
    return list(unique.values()), duplicate_count, missing_identity_count


def calculate_operational_metrics(aircraft_records, crew_records) -> dict:
    """Calculate status and confirmation metrics from unique stored records."""
    aircraft, duplicate_aircraft, missing_aircraft_ids = _latest_unique_records(
        aircraft_records, "resource_id", "updated_at"
    )
    crew, duplicate_crew, missing_crew_ids = _latest_unique_records(
        crew_records, "display_id", "confirmation_at"
    )

    status_counts = Counter(
        record.availability_status for record in aircraft
        if record.availability_status in AIRCRAFT_AVAILABILITY_STATES
    )
    known_status_count = sum(status_counts.values())
    unknown_status_count = len(aircraft) - known_status_count
    not_standby = status_counts["available"] + status_counts["maintenance"] + status_counts["unavailable"]

    confirmation_values = [record.readiness_confirmation for record in aircraft]
    known_confirmations = [value for value in confirmation_values if isinstance(value, bool)]
    readiness_confirmed = sum(value is True for value in known_confirmations)

    crew_status_counts = Counter(
        record.readiness_status for record in crew
        if record.readiness_status in CREW_READINESS_STATES
    )
    known_crew_count = sum(crew_status_counts.values())

    availability_percentages = {
        "available": _percent(status_counts["available"], known_status_count),
        "not_standby": _percent(not_standby, known_status_count),
        "maintenance": _percent(status_counts["maintenance"], known_status_count),
        "standby": _percent(status_counts["standby"], known_status_count),
        "unavailable": _percent(status_counts["unavailable"], known_status_count),
    }
    return {
        "aircraft_total": len(aircraft),
        "aircraft_available": status_counts["available"],
        "aircraft_maintenance": status_counts["maintenance"],
        "aircraft_ready_confirmed": readiness_confirmed,
        "readiness_confirmation_records": len(known_confirmations),
        "readiness_pct": _percent(readiness_confirmed, len(known_confirmations)),
        "availability_pct": availability_percentages["available"],
        "resource_utilization_pct": None,
        "resource_utilization_note": "Unavailable: flight hours, sortie counts, or other usage records are not stored.",
        "crew_total": len(crew),
        "crew_ready": crew_status_counts["ready"],
        "crew_readiness_pct": _percent(crew_status_counts["ready"], known_crew_count),
        "availability": {
            "available": status_counts["available"],
            "maintenance": status_counts["maintenance"],
            "standby": status_counts["standby"],
            "unavailable": status_counts["unavailable"],
            "unknown": unknown_status_count,
            "known_status_count": known_status_count,
            "not_standby": not_standby,
            "percentages": availability_percentages,
        },
        "data_quality": {
            "duplicate_aircraft_records": duplicate_aircraft,
            "missing_aircraft_ids": missing_aircraft_ids,
            "unknown_aircraft_status_records": unknown_status_count,
            "duplicate_crew_records": duplicate_crew,
            "missing_crew_ids": missing_crew_ids,
            "unknown_crew_status_records": len(crew) - known_crew_count,
        },
        "metric_basis": {
            "aircraft_total": {"basis": "count of unique aircraft records in the current dashboard snapshot"},
            "readiness_pct": {"numerator": readiness_confirmed, "denominator": len(known_confirmations), "basis": "readiness_confirmation is true / records with an explicit boolean confirmation"},
            "availability_pct": {"numerator": status_counts["available"], "denominator": known_status_count, "basis": "available / unique aircraft with a recognized availability status"},
            "crew_readiness_pct": {"numerator": crew_status_counts["ready"], "denominator": known_crew_count, "basis": "ready crew / unique crew with a recognized readiness status"},
            "availability_status_percentages": {"denominator": known_status_count, "basis": "each listed status count / unique aircraft with a recognized availability status; unknown statuses are excluded"},
            "rounding": "nearest whole percentage; exact half values round up",
            "resource_utilization_pct": "unavailable because no usage measurements are stored",
        },
    }


def dashboard_summary(db: Session) -> dict:
    aircraft = list(db.scalars(select(Aircraft)).all())
    crew = list(db.scalars(select(Crew)).all())
    maintenance = list(db.scalars(select(MaintenanceRecord)).all())
    schedules = list(db.scalars(select(ScheduleRecord)).all())
    open_alerts = list(db.scalars(select(Alert).where(Alert.status == "open")).all())
    metrics = calculate_operational_metrics(aircraft, crew)
    maintenance_open = sum(m.status in ACTIVE_MAINTENANCE_STATES for m in maintenance)
    return {**metrics,
            "maintenance_open": maintenance_open,
            "maintenance_overdue": sum(m.status == "overdue" for m in maintenance),
            "schedule_active": sum(s.status in {"scheduled", "in_progress"} for s in schedules),
            "alerts_open": len(open_alerts), "data_source_health_pct": None,
            "availability": {**metrics["availability"], "other": metrics["availability"]["unavailable"] + metrics["availability"]["unknown"]},
            "alerts_by_severity": {sev: sum(a.severity == sev for a in open_alerts) for sev in ("low", "medium", "high", "critical")},
            "meta": {"data_mode": "demo" if get_settings().demo_mode else "database", "generated_at": datetime.now(timezone.utc).isoformat()}}


def rows_as_dict(rows, omit=()):
    results=[]
    for row in rows:
        item={col.name:getattr(row,col.name) for col in row.__table__.columns if col.name not in omit}
        for key,value in item.items():
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                item[key]=value.isoformat()
        results.append(item)
    return results


def weather_source_label(records: list[dict], mode: str) -> str:
    sources = {record.get("source") for record in records if record.get("source")}
    if not sources:
        return {"live": "NO LIVE WEATHER DATA", "demo": "NO DEMO DATA", "all": "NO WEATHER DATA"}.get(mode, "NO WEATHER DATA")
    if sources == {"open-meteo"}:
        return "OPEN-METEO FORECAST DATA"
    if sources == {"met-no"}:
        return "MET NORWAY FORECAST DATA"
    if sources <= set(LIVE_WEATHER_SOURCES):
        return "LIVE FORECAST DATA (OPEN-METEO + MET NORWAY)"
    if sources == {"demo"}:
        return "DEMO DATA"
    return "MIXED SOURCES — NOT LIVE-ONLY"


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def trend_series(db: Session, days: int, mode: str = "live") -> dict:
    """Return a source-isolated graph dataset; live is the default mode."""
    settings = get_settings()
    query = select(WeatherObservation).order_by(WeatherObservation.observed_at)
    if mode == "live":
        query = query.where(WeatherObservation.source.in_(LIVE_WEATHER_SOURCES))
    elif mode == "demo":
        query = query.where(WeatherObservation.source == "demo")
    weather = list(db.scalars(query).all())[-days * 24:]
    records = [{
        "time": _iso(record.observed_at),
        "observation_timestamp": _iso(record.observed_at),
        "temperature_c": record.temperature_c,
        "humidity_pct": record.humidity_pct,
        "wind_kmh": record.wind_kmh,
        "precipitation_mm": record.precipitation_mm,
        "source": record.source,
        "data_type": record.data_type or ("demo" if record.source == "demo" else "unknown"),
        "location": record.location,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "fetched_at": _iso(record.fetched_at),
        "created_at": _iso(record.created_at),
    } for record in weather]
    sources = sorted({record["source"] for record in records})
    locations = sorted({record["location"] for record in records if record["location"]})
    fetched = [record["fetched_at"] for record in records if record["fetched_at"]]
    source_label = weather_source_label(records, mode)
    # Open-Meteo and MET Norway are both live providers; only live + demo counts as mixed.
    mixed = len({"demo" if name == "demo" else "live" for name in sources}) > 1
    return {"weather": records, "readiness": [], "meta": {
        "empty": not records,
        "mode": mode,
        "source": sources[0] if len(sources) == 1 else "mixed" if mixed else "live" if sources else None,
        "sources": sources,
        "source_label": source_label,
        "warning": "Multiple weather sources are present; this graph is not live-only." if mixed else None,
        "location": locations[0] if len(locations) == 1 else ", ".join(locations) if locations else settings.open_meteo_location,
        "latitude": records[-1]["latitude"] if records else settings.open_meteo_latitude,
        "longitude": records[-1]["longitude"] if records else settings.open_meteo_longitude,
        "last_updated_at": max(fetched) if fetched else None,
        "timestamp_note": "Provider forecast timestamps; these are not physical sensor observations.",
    }}


MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


async def _fetch_met_no(latitude: float, longitude: float, db: Session, key) -> dict | None:
    """Backup real-time provider (MET Norway, no API key). Returns a payload or None on failure."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0),
                                     headers={"User-Agent": "AEROINTEL-AI/1.0 github.com/sharmarishabh9134-rgb/AEROINTEL-AI"}) as client:
            response = await client.get(MET_NO_URL, params={"lat": round(latitude, 4), "lon": round(longitude, 4)})
            response.raise_for_status()
        body = response.json()
        series = body["properties"]["timeseries"]
        if not isinstance(series, list) or not series:
            raise ValueError("MET Norway response has no timeseries")
        fetched_at = datetime.now(timezone.utc)
        rows = []
        for entry in series[:72]:
            observed = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
            details = entry["data"]["instant"]["details"]
            period = entry["data"].get("next_1_hours") or entry["data"].get("next_6_hours") or {}
            wind_ms = details.get("wind_speed")
            rows.append({"timestamp": observed.isoformat(), "temperature_2m": details.get("air_temperature"),
                         "apparent_temperature": None, "relative_humidity_2m": details.get("relative_humidity"),
                         "precipitation": (period.get("details") or {}).get("precipitation_amount"), "rain": None,
                         "wind_speed_10m": round(wind_ms * 3.6, 1) if isinstance(wind_ms, (int, float)) else None,
                         "wind_gusts_10m": None, "weather_code": None, "visibility": None,
                         "source": "met-no", "data_type": "forecast" if observed > fetched_at else "historical",
                         "latitude": latitude, "longitude": longitude,
                         "fetched_at": fetched_at.isoformat(), "created_at": fetched_at.isoformat()})
        current = min(rows, key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - fetched_at).total_seconds()))
        payload = {"source": "met-no", "is_demo": False, "stale": False,
                   "notice": "Weather provider forecast data from MET Norway (Open-Meteo was unavailable); not a physical sensor or aircraft measurement.",
                   "location": {"name": settings.open_meteo_location, "latitude": latitude, "longitude": longitude},
                   "units": {"temperature": "°C", "humidity": "%", "precipitation": "mm", "wind_speed": "km/h"},
                   "current": current, "hourly": rows, "fetched_at": fetched_at.isoformat()}
        try:
            existing = {o.observed_at.replace(tzinfo=timezone.utc).isoformat(): o for o in db.scalars(select(WeatherObservation).where(
                WeatherObservation.source == "met-no", WeatherObservation.latitude == latitude,
                WeatherObservation.longitude == longitude)).all()}
            for item in rows[:48]:
                ts = datetime.fromisoformat(item["timestamp"])
                values = {"location": settings.open_meteo_location, "temperature_c": item["temperature_2m"],
                          "humidity_pct": item["relative_humidity_2m"], "wind_kmh": item["wind_speed_10m"],
                          "precipitation_mm": item["precipitation"], "source": "met-no", "data_type": item["data_type"],
                          "latitude": latitude, "longitude": longitude, "fetched_at": fetched_at}
                row = existing.get(ts.isoformat())
                if row:
                    for name, value in values.items():
                        setattr(row, name, value)
                else:
                    db.add(WeatherObservation(observed_at=ts, created_at=fetched_at, **values))
            db.commit()
        except Exception as exc:  # saving history is best effort; the live data is still returned
            db.rollback()
            logger.warning("Could not save MET Norway records (%s)", type(exc).__name__)
        _weather_cache[key] = (time.monotonic(), payload)
        return payload
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
        logger.warning("MET Norway weather unavailable (%s: %s)", type(exc).__name__, str(exc)[:300])
        return None


async def fetch_weather(latitude: float, longitude: float, db: Session) -> dict:
    settings = get_settings()
    key = (round(latitude, 4), round(longitude, 4))
    cached = _weather_cache.get(key)
    if cached and time.monotonic() - cached[0] < settings.weather_cache_seconds:
        return cached[1]
    params = {"latitude": latitude, "longitude": longitude, "hourly": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,rain,wind_speed_10m,wind_gusts_10m,weather_code,visibility", "forecast_days": 3, "past_days": 1, "timezone": "UTC"}
    try:
        failed_at = _weather_failures.get(key)
        if failed_at and time.monotonic() - failed_at < 30:
            # The provider just refused us (often HTTP 429 on shared hosting IPs); don't hammer it.
            raise ValueError("Weather provider recently failed; waiting before trying again")
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0),
                                     headers={"User-Agent": "AEROINTEL-AI/1.0 (weather dashboard)"}) as client:
            for attempt in range(2):
                response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
                if getattr(response, "status_code", 200) in (429, 503) and attempt < 1:
                    try:
                        delay = float(response.headers.get("Retry-After", 1))
                    except (TypeError, ValueError):
                        delay = 1.0
                    await asyncio.sleep(min(max(delay, 1.0), 3.0))
                    continue
                break
            response.raise_for_status()
        _weather_failures.pop(key, None)
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Weather response was not a JSON object")
        hourly = body.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("Weather response is missing hourly forecast data")
        times = hourly.get("time")
        if not isinstance(times, list) or not times or not all(isinstance(ts, str) for ts in times):
            raise ValueError("Weather response has invalid forecast timestamps")
        field_names = ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "precipitation", "rain", "wind_speed_10m", "wind_gusts_10m", "weather_code", "visibility")
        fields = {name: hourly.get(name) for name in field_names}
        if any(not isinstance(values, list) or len(values) != len(times) for values in fields.values()):
            raise ValueError("Weather response has missing or mismatched forecast fields")
        fetched_at = datetime.now(timezone.utc)
        rows=[]
        for i, ts in enumerate(times):
            observed = datetime.fromisoformat(ts)
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            rows.append({"timestamp": observed.isoformat(), **{k: vals[i] if i < len(vals) else None for k, vals in fields.items()},
                         "source": "open-meteo", "data_type": "forecast" if observed > fetched_at else "historical",
                         "latitude": latitude, "longitude": longitude, "fetched_at": fetched_at.isoformat(), "created_at": fetched_at.isoformat()})
        current = min(rows, key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - fetched_at).total_seconds())) if rows else {}
        payload = {"source": "open-meteo", "is_demo": False, "stale": False, "notice": "Weather provider forecast data; not a physical sensor or aircraft measurement.", "location": {"name": settings.open_meteo_location, "latitude": latitude, "longitude": longitude}, "units": {"temperature": "°C", "humidity": "%", "precipitation": "mm", "wind_speed": "km/h", "visibility": "m"}, "current": current, "hourly": rows, "fetched_at": fetched_at.isoformat()}
        # Persist provider records by source, coordinates, and forecast timestamp.
        # Updating a matching provider row retains its identity and never touches demo rows.
        by_time = {(o.observed_at.replace(tzinfo=timezone.utc).isoformat(), o.latitude, o.longitude): o
                   for o in db.scalars(select(WeatherObservation).where(
                       WeatherObservation.source == "open-meteo",
                       WeatherObservation.latitude == latitude,
                       WeatherObservation.longitude == longitude,
                   )).all()}
        for item in rows[:48]:
            ts = datetime.fromisoformat(item["timestamp"])
            existing = by_time.get((ts.isoformat(), latitude, longitude))
            values = {"location": settings.open_meteo_location, "temperature_c": item.get("temperature_2m"),
                      "humidity_pct": item.get("relative_humidity_2m"), "wind_kmh": item.get("wind_speed_10m"),
                      "precipitation_mm": item.get("precipitation"), "source": "open-meteo",
                      "data_type": item["data_type"], "latitude": latitude, "longitude": longitude,
                      "fetched_at": fetched_at}
            if existing:
                for key_name, value in values.items():
                    setattr(existing, key_name, value)
                if existing.created_at is None:
                    existing.created_at = fetched_at
            else:
                db.add(WeatherObservation(observed_at=ts, created_at=fetched_at, **values))
        db.commit()
        _weather_cache[key] = (time.monotonic(), payload)
        return payload
    except (httpx.HTTPError, ValueError, KeyError, IntegrityError) as exc:
        if isinstance(exc, IntegrityError):
            # Another request may have inserted the same unique provider point.
            # Clear the failed transaction before reading the committed row.
            db.rollback()
        _weather_failures[key] = time.monotonic()
        logger.warning("Weather provider unavailable (%s: %s)", type(exc).__name__, str(exc)[:300])
        met_payload = await _fetch_met_no(latitude, longitude, db, key)
        if met_payload:
            return met_payload
        history = list(db.scalars(select(WeatherObservation).where(
            WeatherObservation.source.in_(LIVE_WEATHER_SOURCES),
            WeatherObservation.latitude == latitude,
            WeatherObservation.longitude == longitude,
        ).order_by(WeatherObservation.observed_at.desc()).limit(72)).all())
        observations=[]
        for observation in reversed(history):
            observed_at = observation.observed_at
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            observations.append({"timestamp": observed_at.isoformat(), "temperature_2m": observation.temperature_c,
                                 "relative_humidity_2m": observation.humidity_pct, "wind_speed_10m": observation.wind_kmh,
                                 "precipitation": observation.precipitation_mm, "source": observation.source,
                                 "data_type": observation.data_type or "unknown", "latitude": observation.latitude,
                                 "longitude": observation.longitude, "fetched_at": _iso(observation.fetched_at),
                                 "created_at": _iso(observation.created_at)})
        current = min(observations, key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - datetime.now(timezone.utc)).total_seconds())) if observations else {}
        last_fetch = max((row["fetched_at"] for row in observations if row["fetched_at"]), default=None)
        if observations:
            return {"source": observations[-1]["source"], "is_demo": False, "stale": True,
                    "notice": "Live weather providers are unavailable; showing saved provider forecasts. These values may be stale.",
                    "location": {"name": settings.open_meteo_location, "latitude": latitude, "longitude": longitude},
                    "units": {"temperature": "°C", "humidity": "%", "precipitation": "mm", "wind_speed": "km/h"},
                    "current": current, "hourly": observations, "fetched_at": last_fetch}
        if settings.demo_mode:
            # Demo mode: show the fictional sample weather rows so the tab is never empty.
            sample = list(db.scalars(select(WeatherObservation).where(WeatherObservation.source == "demo")
                                     .order_by(WeatherObservation.observed_at.desc()).limit(72)).all())
            sample_rows = []
            for observation in reversed(sample):
                observed_at = observation.observed_at
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                sample_rows.append({"timestamp": observed_at.isoformat(), "temperature_2m": observation.temperature_c,
                                    "relative_humidity_2m": observation.humidity_pct, "wind_speed_10m": observation.wind_kmh,
                                    "precipitation": observation.precipitation_mm, "source": "demo", "data_type": "demo",
                                    "latitude": latitude, "longitude": longitude,
                                    "fetched_at": _iso(observation.fetched_at), "created_at": _iso(observation.created_at)})
            if sample_rows:
                now_utc = datetime.now(timezone.utc)
                sample_current = min(sample_rows, key=lambda item: abs((datetime.fromisoformat(item["timestamp"]) - now_utc).total_seconds()))
                return {"source": "demo", "is_demo": True, "stale": False,
                        "notice": "Live Open-Meteo data could not be reached, so fictional sample weather is shown.",
                        "location": {"name": "Demo region (sample data)", "latitude": latitude, "longitude": longitude},
                        "units": {"temperature": "°C", "humidity": "%", "precipitation": "mm", "wind_speed": "km/h"},
                        "current": sample_current, "hourly": sample_rows, "fetched_at": None}
        return {"source": "unavailable", "is_demo": False, "stale": False,
                "notice": "Open-Meteo is unavailable and no saved provider records exist.",
                "location": {"name": settings.open_meteo_location, "latitude": latitude, "longitude": longitude},
                "units": {"temperature": "°C", "humidity": "%", "precipitation": "mm", "wind_speed": "km/h"},
                "current": {}, "hourly": [], "fetched_at": None}


def classify_analyzer_intent(question: str | None) -> str:
    """Route non-analysis messages before any dashboard data reaches a provider."""
    text = (question or "").strip().lower()
    text = re.sub(r"[\s.!?,;:]+$", "", text)
    if not text:
        return "empty"

    if re.fullmatch(r"(?:hey+|hello+|hi+|howdy|good\s+(?:morning|afternoon|evening))(?:\s+there)?", text):
        return "greeting"
    if re.fullmatch(r"(?:thanks+|thank\s+you|thx)(?:\s+(?:so\s+much|a\s+lot))?", text):
        return "thanks"

    # Allow a short greeting or thanks followed by a genuine dashboard question.
    remainder = re.sub(r"^(?:hey+|hello+|hi+|howdy|good\s+(?:morning|afternoon|evening))\b[\s,!.?]*", "", text)
    remainder = re.sub(r"^(?:thanks+|thank\s+you|thx)\b[\s,!.?]*", "", remainder)
    if not remainder:
        return "thanks" if re.search(r"\b(?:thanks+|thank\s+you|thx)\b", text) else "greeting"

    dashboard_terms = re.compile(
        r"\b(?:dashboard|snapshot|overview|aircraft|availability|available|standby|"
        r"readiness|ready|crew|maintenance|alert|alerts|schedule|weather|inventory|"
        r"resource|utilization|database|records?|metrics?|data|source|open issues?|"
        r"machine learning|ml analytics|model|prediction|predictions|dataset|class distribution|"
        r"accuracy|precision|recall|f1|confusion matrix)\b"
    )
    if dashboard_terms.search(remainder):
        return "analysis"
    return "unrelated"


def _analyzer_response_metadata(source: str, provider_configured: bool, generated_at: str) -> dict:
    labels = {"gemini": "Gemini AI", "rules-fallback": "Rule-based fallback", "intent-handler": "Intent handler"}
    return {
        "source": source,
        "source_label": labels[source],
        "provider": {"name": "Gemini", "configured": provider_configured, "used": source == "gemini"},
        "ai_generated": source == "gemini",
        "verified": False,
        "generated_at": generated_at,
    }


def _analysis_facts(context: dict) -> list[dict]:
    """Present calculations from the backend snapshot with their basis and time."""
    timestamp = context.get("meta", {}).get("generated_at")
    basis = context.get("metric_basis", {})
    aircraft_total = context.get("aircraft_total")
    total_basis = basis.get("aircraft_total", {}).get("basis", "Count of unique aircraft records in the current backend dashboard snapshot.")
    facts = [{
        "label": "Aircraft records",
        "value": "Unavailable: aircraft count was not supplied by the backend snapshot." if aircraft_total is None else str(aircraft_total),
        "basis": total_basis,
        "timestamp": timestamp,
    }]
    for key, label in (("readiness_pct", "Aircraft readiness confirmation"),
                       ("availability_pct", "Aircraft availability"),
                       ("crew_readiness_pct", "Crew readiness")):
        metric = basis.get(key, {})
        numerator, denominator = metric.get("numerator"), metric.get("denominator")
        value = context.get(key)
        if value is None or denominator in (None, 0):
            display = "Unavailable: no records with a valid denominator."
        else:
            display = f"{numerator}/{denominator} ({value}%)."
        facts.append({"label": label, "value": display, "basis": metric.get("basis", "Backend dashboard calculation."), "timestamp": timestamp})

    for label, value, basis_text in (
        ("Open maintenance records", context.get("maintenance_open"), "Count of maintenance records with status open, in_progress, or overdue."),
        ("Open alerts", context.get("alerts_open"), "Count of alert records with status open."),
        ("Active schedules", context.get("schedule_active"), "Count of schedule records with status scheduled or in_progress."),
    ):
        facts.append({"label": label, "value": "Unavailable." if value is None else str(value), "basis": basis_text, "timestamp": timestamp})
    sources = context.get("data_sources", {})
    database_source = sources.get("database", {})
    facts.append({
        "label": "Operational database source",
        "value": f"{database_source.get('status', 'unknown')} · {database_source.get('mode', 'mode unavailable')}",
        "basis": "Database connection and application data mode reported by the backend.",
        "timestamp": timestamp,
    })
    weather = sources.get("weather", {})
    if weather.get("source") in LIVE_WEATHER_SOURCES:
        measurements = weather.get("measurements", {})
        detail = ", ".join(f"{key}={value}" for key, value in measurements.items() if value is not None) or "weather values unavailable"
        value = (f"{weather.get('status', 'unknown')} {'MET Norway' if weather.get('source') == 'met-no' else 'Open-Meteo'} provider record · {weather.get('data_type', 'type unavailable')} · "
                 f"{weather.get('location', 'location unavailable')} ({weather.get('latitude')}, {weather.get('longitude')}) · {detail}")
        source_basis = "Saved provider record; forecast values are not physical sensor measurements. " + weather.get("freshness_rule", "")
    else:
        value = f"{weather.get('status', 'unavailable')} · {weather.get('notice', 'No saved provider record.') }"
        source_basis = "Open-Meteo provider records only; demo weather records are excluded."
    facts.append({"label": "Weather data source", "value": value,
                  "basis": source_basis,
                  "timestamp": weather.get("last_update") or timestamp,
                  "observation_timestamp": weather.get("observation_timestamp")})
    return facts


def _metric_sentence(context: dict, key: str, metric_label: str) -> str:
    """Render an aircraft metric only from its backend numerator and denominator."""
    metric = context.get("metric_basis", {}).get(key, {})
    numerator, denominator = metric.get("numerator"), metric.get("denominator")
    percentage = context.get(key)
    if numerator is None or denominator in (None, 0) or percentage is None:
        return f"Aircraft {metric_label} data is unavailable because the snapshot has no valid records for that metric."
    return f"{numerator} aircraft are {metric_label} ({percentage}%)."


def _direct_aircraft_answer(context: dict, question: str | None) -> str | None:
    """Answer simple aircraft questions before optional provider interpretation."""
    text = (question or "").strip().lower()
    if not re.search(r"\baircraft\b", text):
        return None

    asks_count = bool(re.search(r"\b(?:how many|number of|count|total)\b", text))
    asks_availability = bool(re.search(r"\b(?:available|availability)\b", text))
    asks_readiness = bool(re.search(r"\b(?:readiness|ready|confirmed|confirmation)\b", text))
    total = context.get("aircraft_total")
    if asks_count and not asks_availability and not asks_readiness:
        first = (f"You have {total} aircraft records in the current dashboard snapshot."
                 if total is not None else
                 "The aircraft count is unavailable in the current dashboard snapshot because the backend did not provide it.")
        readiness = _metric_sentence(context, "readiness_pct", "readiness-confirmed")
        availability = _metric_sentence(context, "availability_pct", "marked available")
        if "data is unavailable" in readiness or "data is unavailable" in availability:
            return " ".join((first, readiness, availability))
        return f"{first} {readiness[:-1]}, and {availability}"
    if asks_availability:
        return _metric_sentence(context, "availability_pct", "marked available")
    if asks_readiness:
        return _metric_sentence(context, "readiness_pct", "readiness-confirmed")
    return None


def _intent_response(intent: str, provider_configured: bool) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    messages = {
        "empty": "Enter a question about the dashboard to get started.",
        "greeting": "Hey! I can help explain the dashboard’s aircraft, crew, maintenance, schedule, alert, weather, and inventory data.",
        "thanks": "You’re welcome! Ask me about the dashboard whenever you’re ready.",
        "unrelated": "I can help with the data shown in this dashboard, such as aircraft availability, crew readiness, maintenance, schedules, alerts, weather, or inventory estimates.",
    }
    return {
        "intent": intent,
        "summary": messages[intent],
        "calculated_data": [],
        "interpretation": None,
        "recommendations": [],
        "limitations": "No dashboard analysis was run for this message.",
        **_analyzer_response_metadata("intent-handler", provider_configured, generated_at),
    }


async def analyze(context: dict, question: str | None = None) -> dict:
    settings = get_settings()
    provider_configured = bool(settings.gemini_api_key)
    intent = classify_analyzer_intent(question)
    if intent != "analysis":
        return _intent_response(intent, provider_configured)

    generated_at = datetime.now(timezone.utc).isoformat()
    direct_answer = _direct_aircraft_answer(context, question)
    safe_context = json.dumps(context, ensure_ascii=True, default=str)[:12000]
    prompt = (
        "You are interpreting a current AEROINTEL administrative dashboard snapshot. It may contain fictional demo records. "
        "Never give tactical, targeting, strike, route-planning, or operational military advice. "
        "The supplied snapshot is the only source of facts. Do not invent metrics, alerts, dates, causes, confidence scores, or recommendations. "
        "The backend will display calculated values separately, so do not restate or alter them. "
        "Return JSON with exactly these keys: interpretation (string, or empty string if no defensible interpretation) and recommendations (array of strings, possibly empty). "
        "Recommendations must be administrative, directly supported by supplied records, and state the supporting record/count. "
        "Do not claim the records are real-world operational readiness or that interpretations are verified.\n"
        "Snapshot: " + safe_context + "\nQuestion: " + json.dumps(question, ensure_ascii=True)[:1200]
    )
    if provider_configured:
        try:
            from google import genai
            client = genai.Client(api_key=settings.gemini_api_key, http_options={"timeout": 12_000})
            result = await asyncio.to_thread(client.models.generate_content, model=settings.gemini_model, contents=prompt, config={"response_mime_type": "application/json"})
            parsed = json.loads(result.text)
            if (not isinstance(parsed, dict)
                    or not isinstance(parsed.get("interpretation"), str)
                    or not isinstance(parsed.get("recommendations"), list)
                    or not all(isinstance(item, str) for item in parsed["recommendations"])):
                raise ValueError("Unexpected analysis shape")
            return {
                "intent": "analysis",
                "summary": direct_answer or "Analysis uses the current backend dashboard snapshot.",
                "calculated_data": _analysis_facts(context),
                "interpretation": parsed["interpretation"],
                "recommendations": parsed["recommendations"],
                "limitations": "Stored data may be fictional demo data. Calculations are backend-derived; Gemini interpretations are not independently verified.",
                **_analyzer_response_metadata("gemini", True, generated_at),
            }
        except Exception as exc:
            logger.warning("Gemini analysis unavailable (%s)", type(exc).__name__)

    recommendations = []
    if context.get("maintenance_open", 0):
        recommendations.append(f"Review the {context['maintenance_open']} stored open maintenance records.")
    if context.get("alerts_open", 0):
        recommendations.append(f"Review the {context['alerts_open']} stored open alerts.")
    return {
        "intent": "analysis",
        "summary": direct_answer or "Calculated values below come directly from the current backend dashboard snapshot.",
        "calculated_data": _analysis_facts(context),
        "interpretation": None,
        "recommendations": recommendations,
        "limitations": "Rule-based fallback; no AI model response was used. Stored records may be fictional demo data and are not a verified operational assessment.",
        **_analyzer_response_metadata("rules-fallback", provider_configured, generated_at),
    }
