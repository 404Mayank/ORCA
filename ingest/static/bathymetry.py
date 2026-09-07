"""Bathymetry: seabed depth, fetched once and cached.

Source: ``srtm30plus`` on NOAA CoastWatch ERDDAP -- global, ~0.0083 deg (~900 m),
keyless. GEBCO 2024 is what CLAUDE.md names; it is a 7 GB NetCDF behind a
registration form, and SRTM30_PLUS is the same quantity at a resolution finer
than anything else in this project. Swap later if GEBCO is obtained.

Static, so unlike the forecast layers this is fetched **once** and kept. It has
no time axis, which is why it does not go through ``erddap.fetch_grid()``.

Sign convention, stated because it causes real bugs: ``z`` is **elevation**, so
the sea is **negative**. Depth is ``-z``. A routing cost that treated z as
depth would send a boat over a mountain.
"""

from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ingest.sources.erddap import SERVERS, ErddapError, _get_json

__all__ = ["Bathymetry", "fetch", "load_cached", "CACHE_PATH"]

CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "static" / "bathymetry.json"

DATASET_ID = "srtm30plus"
VARIABLE = "z"
NATIVE_RES_DEG = 1.0 / 120.0
#: Stride 6 -> ~0.05 deg, the ORCA target grid. Finer buys nothing: the routing
#: grid is 0.05 deg and a boat is not steered to 900 m precision.
STRIDE = 6

_KM_PER_DEG = 111.19


@dataclass(frozen=True)
class Bathymetry:
    """Depth on a regular grid. ``depth_m`` is positive down; land is None."""

    url: str
    fetched_at: datetime
    #: (lat, lon) -> depth_m, positive down. Land cells are omitted entirely.
    cells: dict[tuple[float, float], float]
    resolution_deg: float

    def depth_at(self, lat: float, lon: float, max_km: float = 8.0) -> float | None:
        """Depth at a point, or None for land / no nearby sample."""
        if not self.cells:
            return None
        key = min(
            self.cells,
            key=lambda c: (c[0] - lat) ** 2 + ((c[1] - lon) * math.cos(math.radians(lat))) ** 2,
        )
        dlat = (key[0] - lat) * _KM_PER_DEG
        dlon = (key[1] - lon) * _KM_PER_DEG * math.cos(math.radians(lat))
        return self.cells[key] if math.hypot(dlat, dlon) <= max_km else None

    def is_navigable(self, lat: float, lon: float, min_depth_m: float) -> bool:
        depth = self.depth_at(lat, lon)
        return depth is not None and depth >= min_depth_m


def fetch(bbox: tuple[float, float, float, float]) -> Bathymetry:
    """Pull bathymetry for the box and cache it. Idempotent."""
    west, south, east, north = bbox
    selector = (
        f"{VARIABLE}[({south}):{STRIDE}:({north})][({west}):{STRIDE}:({east})]"
    )
    url = (
        f"{SERVERS['coastwatch']}/griddap/{DATASET_ID}.json"
        f"?{urllib.parse.quote(selector, safe='')}"
    )
    payload = _get_json(url)
    table = payload["table"]
    columns = table["columnNames"]
    lat_i, lon_i, val_i = columns.index("latitude"), columns.index("longitude"), len(columns) - 1

    cells: dict[tuple[float, float], float] = {}
    for row in table["rows"]:
        z = row[val_i]
        if z is None or z >= 0:
            continue  # land or shoreline; omitted, never stored as depth 0
        cells[(round(float(row[lat_i]), 4), round(float(row[lon_i]), 4))] = round(-float(z), 2)

    bathymetry = Bathymetry(
        url=url,
        fetched_at=datetime.now(timezone.utc),
        cells=cells,
        resolution_deg=NATIVE_RES_DEG * STRIDE,
    )
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "url": url,
                "fetched_at": bathymetry.fetched_at.isoformat(),
                "resolution_deg": bathymetry.resolution_deg,
                "cells": [[k[0], k[1], v] for k, v in cells.items()],
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(CACHE_PATH)
    return bathymetry


def load_cached() -> Bathymetry | None:
    """The cached grid, or None. None means unknown depth, never 'deep enough'."""
    if not CACHE_PATH.exists():
        return None
    try:
        record = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return Bathymetry(
            url=record["url"],
            fetched_at=datetime.fromisoformat(record["fetched_at"]),
            cells={(c[0], c[1]): c[2] for c in record["cells"]},
            resolution_deg=record["resolution_deg"],
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def refresh(bbox: tuple[float, float, float, float]) -> list[str]:
    if CACHE_PATH.exists():
        return []  # static: fetched once
    try:
        fetch(bbox)
    except ErddapError as exc:
        return [f"bathymetry: {exc}"]
    return []
