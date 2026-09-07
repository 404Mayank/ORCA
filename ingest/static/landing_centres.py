"""Landing-centre table: seed gazetteer plus the INCOIS set when it lands.

Today this module only documents the merge contract. The INCOIS fish landing
centre points are not yet obtained (see config/datasets.yaml, source
`incois_landing_centres`, status `wanted`), so every tool reads the seed from
`config/bbox.yaml` via `tools.geo.nearest._gazetteer`.

When the set arrives as `data/static/landing_centres.geojson` (preferred) or
`.csv` with `name,lat,lon` columns, :func:`load_table` returns those points
with per-point source `incois_landing_centres` and confidence 1.0, and tools
switch by calling it first and falling back to the seed. Until then it returns
an empty dict and nothing changes -- a missing official register degrades to
the provisional seed openly (match_confidence < 1.0 everywhere), never to a
silent substitution.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

__all__ = ["TABLE_PATHS", "load_table"]

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "static"

#: Tried in order. GeoJSON first: it carries its own CRS and properties.
TABLE_PATHS = (
    DATA_DIR / "landing_centres.geojson",
    DATA_DIR / "landing_centres.json",
    DATA_DIR / "landing_centres.csv",
)


def _from_geojson(payload: dict[str, Any]) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for feature in payload.get("features", []):
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        coords = geom.get("coordinates") or []
        name = str(props.get("name") or "").strip()
        if not name or len(coords) < 2:
            continue
        table[name.lower()] = {
            "name": name,
            "lat": float(coords[1]),
            "lon": float(coords[0]),
            "source": "incois_landing_centres",
            "confidence": 1.0,
        }
    return table


def _from_rows(rows: list[dict[str, str]]) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for row in rows:
        name = (row.get("name") or "").strip()
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name:
            continue
        table[name.lower()] = {
            "name": name,
            "lat": lat,
            "lon": lon,
            "source": "incois_landing_centres",
            "confidence": 1.0,
        }
    return table


def load_table() -> dict[str, dict]:
    """The official landing-centre table, or {} when not yet ingested."""
    for path in TABLE_PATHS:
        if not path.exists():
            continue
        try:
            if path.suffix == ".csv":
                with path.open(encoding="utf-8") as handle:
                    return _from_rows(list(csv.DictReader(handle)))
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
                return _from_geojson(payload)
            if isinstance(payload, list):
                return _from_rows(payload)
        except (json.JSONDecodeError, ValueError, OSError):
            continue
    return {}
