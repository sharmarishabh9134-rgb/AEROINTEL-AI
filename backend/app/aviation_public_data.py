"""Best-effort public aviation feeds; provider outages are represented as metadata."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

_cache: dict[str, tuple[float, Any]] = {}
_lock = asyncio.Lock()
_TTL_SECONDS = 300


async def _fetch_json(url: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True) as client:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await client.get(url, params=params, headers={"User-Agent": "AEROINTEL-AI/1.0 public-data dashboard"})
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.2)
        raise RuntimeError(type(last_error).__name__ if last_error else "provider error")


async def _cached(key: str, fetcher):
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1], True, None
    async with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < _TTL_SECONDS:
            return cached[1], True, None
        try:
            value = await fetcher()
            _cache[key] = (time.monotonic(), value)
            return value, False, None
        except Exception as exc:  # provider failures must never break the dashboard
            if cached:
                return cached[1], True, f"Provider unavailable; showing cached data ({type(exc).__name__})."
            return None, False, f"Provider unavailable ({type(exc).__name__}); saved dashboard data remains available."


async def fetch_public_aviation_data(airports: list[str]) -> dict[str, Any]:
    codes = sorted({code.upper() for code in airports if len(code) == 4 and code.isalpha()})[:10]
    metar_key = "metar:" + ",".join(codes)
    metars, metar_cached, metar_notice = await _cached(
        metar_key,
        lambda: _fetch_json("https://aviationweather.gov/api/data/metar", {"ids": ",".join(codes), "format": "json", "taf": "false"}),
    ) if codes else ([], False, "No valid airport ICAO codes supplied.")

    # Return only a coarse count for the India bounding region: no callsigns or positions.
    opensky, opensky_cached, opensky_notice = await _cached(
        "opensky:india:aggregate",
        lambda: _fetch_json("https://opensky-network.org/api/states/all", {
            "lamin": 6, "lomin": 68, "lamax": 36, "lomax": 98,
        }),
    )
    state_vectors = opensky.get("states") if isinstance(opensky, dict) else None
    return {
        "data": {
            "metars": metars if isinstance(metars, list) else [],
            "metar_source": "aviationweather.gov" if isinstance(metars, list) and metars else "unavailable",
            "metar_cached": metar_cached,
            "opensky_region": "India bounding region",
            "opensky_aircraft_count": len(state_vectors) if isinstance(state_vectors, list) else None,
            "opensky_source": "OpenSky Network aggregate" if isinstance(state_vectors, list) else "unavailable",
            "opensky_cached": opensky_cached,
        },
        "notices": [notice for notice in (metar_notice, opensky_notice) if notice],
    }
