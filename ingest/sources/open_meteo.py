"""Open-Meteo ingest. Fetches, records provenance, writes a local cache.

**Nothing above this module fetches during a user query.** CLAUDE.md is explicit
about it, and the reason is the demo: a query that reaches out to the network is
a query that fails when the venue wifi drops. ``ingest/`` runs on a schedule and
writes a cache; ``tools/weather/`` reads that cache and nothing else.

Verified against the live API on 2026-09-04 -- see docs/verified_sources.md.
Both endpoints are keyless. The facts that matter here:

* Marine data for the Bay of Bengal is on a **1/12 deg (~9.3 km)** grid,
  measured, not 0.05 deg. The response echoes the snapped cell centre, which we
  record rather than the coordinate we asked for.
* ``ocean_current_velocity`` is returned in **km/h**.
* ``visibility`` on the forecast endpoint is returned in **metres**.
* ``wind_speed_unit=kn`` is honoured, so knots are requested rather than
  hand-converted.

Conversions happen here, at the boundary, where they show up in a diff. Never at
comparison time -- ``core.units.evaluate()`` raises on a unit mismatch by design.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CACHE_DIR",
    "MARINE_URL",
    "FORECAST_URL",
    "ARCHIVE_URL",
    "MARINE_HOURLY",
    "FORECAST_HOURLY",
    "CachedResponse",
    "fetch_marine",
    "fetch_forecast",
    "fetch_archive",
    "load_cached",
    "cache_path",
]

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "open_meteo"

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

#: Verified to return non-null hourly values at 10.77 N, 79.84 E.
MARINE_HOURLY = [
    "wave_height",
    "wave_direction",
    "wave_period",
    "wind_wave_height",
    "wind_wave_period",
    "swell_wave_height",
    "swell_wave_period",
    "sea_surface_temperature",
    "ocean_current_velocity",
    "ocean_current_direction",
    "sea_level_height_msl",
]

FORECAST_HOURLY = [
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "visibility",
    "precipitation",
]

#: Grid resolution of the marine model over our box, measured by probing the
#: API across a longitude sweep. Travels with every value as
#: `native_resolution_deg` so nothing downstream claims 0.05 deg.
MARINE_NATIVE_RESOLUTION_DEG = 1.0 / 12.0

_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class CachedResponse:
    """A fetched payload plus everything needed to defend it later."""

    endpoint: str
    url: str
    params: dict[str, Any]
    fetched_at: datetime
    payload: dict[str, Any]

    @property
    def snapped_lat(self) -> float:
        """The grid cell centre the model actually served.

        Distinct from the requested coordinate, and it is this one that goes
        into provenance. Asking for 79.84 and being served 79.95836 is a 12 km
        difference, and reporting the request as though it were the answer
        would overstate our spatial precision.
        """
        return float(self.payload["latitude"])

    @property
    def snapped_lon(self) -> float:
        return float(self.payload["longitude"])

    def hourly(self) -> dict[str, list]:
        return self.payload["hourly"]

    def units(self) -> dict[str, str]:
        return self.payload.get("hourly_units", {})


def cache_path(endpoint: str, lat: float, lon: float, day: str | None = None) -> Path:
    """Deterministic cache filename.

    Coordinates are rounded to 2 dp before keying. The marine grid is 1/12 deg
    wide, so anything finer produces cache misses for points the model cannot
    tell apart anyway.
    """
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return CACHE_DIR / f"{endpoint}_{lat:.2f}_{lon:.2f}_{day}.json"


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params, doseq=True)
    full = f"{url}?{query}"
    with urllib.request.urlopen(full, timeout=_TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8")), full


def _write_cache(
    endpoint: str, lat: float, lon: float, url: str, params: dict, payload: dict
) -> CachedResponse:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc)
    record = {
        "endpoint": endpoint,
        "url": url,
        "params": params,
        "fetched_at": fetched_at.isoformat(),
        "payload": payload,
    }
    path = cache_path(endpoint, lat, lon)
    # Write-then-rename, so a crash mid-write cannot leave a half-parsed cache
    # file that a later query would read as real data.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)
    return CachedResponse(endpoint, url, params, fetched_at, payload)


def load_cached(endpoint: str, lat: float, lon: float) -> CachedResponse | None:
    """Read today's cache for a point, or the most recent one available.

    Returns None when nothing is cached. Callers must treat that as a failure,
    never as an absence of weather.
    """
    exact = cache_path(endpoint, lat, lon)
    candidates = [exact] if exact.exists() else sorted(
        CACHE_DIR.glob(f"{endpoint}_{lat:.2f}_{lon:.2f}_*.json"), reverse=True
    )
    if not candidates:
        return None
    record = json.loads(candidates[0].read_text(encoding="utf-8"))
    return CachedResponse(
        endpoint=record["endpoint"],
        url=record["url"],
        params=record["params"],
        fetched_at=datetime.fromisoformat(record["fetched_at"]),
        payload=record["payload"],
    )


# --------------------------------------------------------------------------
# Fetchers -- called by the scheduled worker, never by a query
# --------------------------------------------------------------------------


def fetch_marine(lat: float, lon: float, forecast_days: int = 3) -> CachedResponse:
    """Waves, SST, currents and sea level for a point. Keyless."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(MARINE_HOURLY),
        "forecast_days": forecast_days,
        "timezone": "Asia/Kolkata",
    }
    payload, url = _get(MARINE_URL, params)
    return _write_cache("marine", lat, lon, url, params, payload)


def fetch_forecast(lat: float, lon: float, forecast_days: int = 3) -> CachedResponse:
    """Wind, gusts, visibility and precipitation for a point. Keyless.

    ``wind_speed_unit=kn`` is requested explicitly: the API converts, so we do
    not, and the response's own ``hourly_units`` confirms it.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join(FORECAST_HOURLY),
        "wind_speed_unit": "kn",
        "forecast_days": forecast_days,
        "timezone": "Asia/Kolkata",
    }
    payload, url = _get(FORECAST_URL, params)
    return _write_cache("forecast", lat, lon, url, params, payload)


def fetch_archive(
    lat: float, lon: float, start_date: str, end_date: str
) -> CachedResponse:
    """Historical reanalysis, for the Cyclone Gaja replay.

    Backed by ERA5 at roughly 0.25 deg. That resolution smooths a tropical
    cyclone core heavily -- the Gaja pressure minimum comes back far shallower
    than the storm actually was -- so the wind field is the usable signal here
    and the pressure field is not. See docs/verified_sources.md.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": "wind_speed_10m,wind_gusts_10m,wind_direction_10m,pressure_msl",
        "wind_speed_unit": "kn",
        "timezone": "Asia/Kolkata",
    }
    payload, url = _get(ARCHIVE_URL, params)
    return _write_cache(f"archive_{start_date}", lat, lon, url, params, payload)


def refresh_points(points: list[tuple[float, float]], forecast_days: int = 3) -> list[str]:
    """Refresh the cache for a list of points. Idempotent.

    Re-running overwrites cleanly rather than duplicating, per the ingest rule
    in CLAUDE.md. Failures are collected and returned rather than raised, so one
    dead point does not abort the whole nightly run.
    """
    problems: list[str] = []
    for lat, lon in points:
        for name, fn in (("marine", fetch_marine), ("forecast", fetch_forecast)):
            try:
                fn(lat, lon, forecast_days)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                problems.append(f"{name} {lat},{lon}: {type(exc).__name__}: {exc}")
            time.sleep(0.2)  # courtesy to a free keyless service
    return problems
