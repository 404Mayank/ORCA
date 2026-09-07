"""GET /geo/boundaries -- static maritime geometry for the map.

The geofence tool knows the boundary but its output carries no coordinates
(`ZoneHit` has names and distances, not polylines), so the frontend map
could only draw an empty box. This endpoint serves the IMBL polyline from
the SAME digitised treaty source the tool checks against
(`ingest/static/boundaries.py`), so the drawn line and the verdict can
never disagree through drift.

Static data: cached by browsers, no fetch during a question involved.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from core import config
from ingest.static.boundaries import GEOMETRY_NOTES, imbl_linestring

router = APIRouter(tags=["geo"])


class BoundariesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imbl: list[list[float]] = Field(
        description="IMBL as [lon, lat] positions, EPSG:4326. Same line the geofence tool tests."
    )
    source: str = Field(description="Where the geometry was digitised from.")
    notes: list[str] = Field(description="Caveats, e.g. straight segments between treaty positions.")
    bbox: list[float] = Field(description="Study box [west, south, east, north].")


@router.get("/geo/boundaries", response_model=BoundariesResponse)
def boundaries() -> dict[str, Any]:
    """The treaty line, for drawing. Cheap, no I/O, never changes at runtime."""
    line = imbl_linestring()
    return {
        "imbl": [[round(x, 5), round(y, 5)] for x, y in line.coords],
        "source": "1974/1976 India–Sri Lanka agreements, digitised in ingest/static/boundaries.py",
        "notes": GEOMETRY_NOTES,
        "bbox": list(config.bbox()),
    }
