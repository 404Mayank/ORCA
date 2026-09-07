"""Geofence intersection: EEZ, the India-Sri Lanka IMBL, and the Gulf of Mannar MPA.

SAFETY-CRITICAL in the legal sense rather than the meteorological one. Crossing
the IMBL is what gets a Tamil Nadu fisherman arrested, and the LLM must never
be the thing that decides whether a point is on the wrong side of it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import GeoPoint, ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class ZoneHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str
    zone_name: str
    zone_type: str = Field(description="eez | imbl | mpa | restricted")
    relation: str = Field(description="inside | crosses | within_buffer")
    distance_km: float | None = Field(default=None, description="To the boundary. Negative means inside.")
    authority: str | None = None


class GeofenceCheckIn(ToolInput):
    points: list[GeoPoint] = Field(min_length=1, description="A single point, or a path to test for crossings.")
    buffer_km: float = Field(default=0.0, ge=0.0, description="Warn when this close to a boundary, not only on crossing it.")
    zone_types: list[str] = Field(default_factory=lambda: ["imbl", "mpa", "restricted"])


class GeofenceCheckOut(ToolOutput):
    hits: list[ZoneHit] = Field(default_factory=list)
    clear: bool = Field(description="True when no zone was intersected. An empty hits list with clear=True is a citable negative finding.")
    zones_checked: list[str] = Field(default_factory=list, description="Zone types actually tested. `clear` means clear of THESE, not of everything requested.")
    notes: list[str] = Field(default_factory=list, description="Geometry caveats and unavailable zone types, surfaced rather than dropped.")


register(
    "geofence_check",
    description="Test a point or path against maritime boundaries and protected areas.",
    input_model=GeofenceCheckIn,
    output_model=GeofenceCheckOut,
    agent=AgentGroup.GEOSPATIAL,
    safety_critical=True,
    notes="IMBL digitised from the 1974/1976 treaty texts (ingest/static/boundaries.py). MPA/EEZ/restricted pending keyless geometry; see config/datasets.yaml.",
)


# ==========================================================================
# Implementation
# ==========================================================================

from functools import lru_cache

from pyproj import Geod
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from core.schemas.tool_io import Provenance, ToolStatus
from ingest.static.boundaries import (
    GEOMETRY_NOTES,
    imbl_linestring,
    indian_waters_polygon,
)
from tools import registry

_GEOD = Geod(ellps="WGS84")

#: Zone types we can actually test today. `GeofenceCheckOut.zones_checked`
#: reports this rather than the caller's request, so a partial check can never
#: be read as a full one -- "clear" means clear of what we looked at, and the
#: answer says what that was.
IMPLEMENTED_ZONE_TYPES: set[str] = {"imbl"}

#: Requested but not available. Recorded so the gap is visible in the response
#: instead of silently reducing to nothing.
UNAVAILABLE_ZONE_TYPES: dict[str, str] = {
    "mpa": "Gulf of Mannar MPA geometry not yet obtained (Protected Planet/WDPA).",
    "restricted": "No restricted-area source identified yet.",
    "eez": "EEZ geometry not yet obtained (Marine Regions v12).",
}


@lru_cache(maxsize=1)
def _imbl():
    return imbl_linestring()


@lru_cache(maxsize=1)
def _indian_waters():
    return indian_waters_polygon()


def _geodesic_km_to_line(lat: float, lon: float, line) -> tuple[float, Point]:
    """True distance from a point to the boundary, plus the closest point on it.

    Shapely works in degrees, which are not a distance. The nearest point is
    found in degree space -- fine, because the error in *which* point is
    nearest is negligible at these scales -- and the distance to it is then
    measured geodesically, because "23 km from the line" has to be a real
    number a fisherman can act on.
    """
    here = Point(lon, lat)
    _, on_line = nearest_points(here, line)
    _, _, metres = _GEOD.inv(lon, lat, on_line.x, on_line.y)
    return metres / 1000.0, on_line


def geofence_check(args: GeofenceCheckIn) -> GeofenceCheckOut:
    """Test a point or path against the India-Sri Lanka maritime boundary.

    This is the query that keeps people out of prison. Crossing the IMBL is
    what gets a Tamil Nadu fisherman arrested by the Sri Lankan Navy, and the
    determination of which side a point falls on is made by geometry digitised
    from the treaty text -- never by a language model.
    """
    line = _imbl()
    indian = _indian_waters()

    requested = set(args.zone_types)
    checked = sorted(requested & IMPLEMENTED_ZONE_TYPES)
    unavailable = sorted(requested - IMPLEMENTED_ZONE_TYPES)

    hits: list[ZoneHit] = []
    notes: list[str] = list(GEOMETRY_NOTES)
    for zone_type in unavailable:
        notes.append(
            UNAVAILABLE_ZONE_TYPES.get(zone_type, f"Unknown zone type {zone_type!r}.")
        )

    if "imbl" in checked:
        for point in args.points:
            here = Point(point.lon, point.lat)
            distance_km, _ = _geodesic_km_to_line(point.lat, point.lon, line)
            inside_india = indian.contains(here)

            if not inside_india:
                hits.append(
                    ZoneHit(
                        zone_id="imbl_lka_ind",
                        zone_name="India-Sri Lanka International Maritime Boundary",
                        zone_type="imbl",
                        relation="inside",
                        distance_km=-round(distance_km, 3),
                        authority="1974/1976 India-Sri Lanka agreements",
                    )
                )
            elif args.buffer_km > 0 and distance_km <= args.buffer_km:
                hits.append(
                    ZoneHit(
                        zone_id="imbl_lka_ind",
                        zone_name="India-Sri Lanka International Maritime Boundary",
                        zone_type="imbl",
                        relation="within_buffer",
                        distance_km=round(distance_km, 3),
                        authority="1974/1976 India-Sri Lanka agreements",
                    )
                )

        # A path is more than its vertices: two points can both be on the
        # Indian side while the leg between them cuts the corner across the
        # boundary. Checking only the endpoints would clear exactly the track
        # that gets a boat detained.
        if len(args.points) > 1:
            track = LineString([(p.lon, p.lat) for p in args.points])
            if track.intersects(line):
                hits.append(
                    ZoneHit(
                        zone_id="imbl_lka_ind",
                        zone_name="India-Sri Lanka International Maritime Boundary",
                        zone_type="imbl",
                        relation="crosses",
                        distance_km=0.0,
                        authority="1974/1976 India-Sri Lanka agreements",
                    )
                )

    status = ToolStatus.OK if not unavailable else ToolStatus.DEGRADED
    return GeofenceCheckOut(
        provenance=Provenance(
            source="treaty_digitised",
            source_url="https://www.un.org/Depts/los/LEGISLATIONANDTREATIES/PDFFILES/TREATIES/LKA-IND1976MB.PDF",
            authority="UN DOALOS Delimitation Treaties Infobase",
            native_units="degrees",
        ),
        status=status,
        error=None if not unavailable else f"zone types not available: {unavailable}",
        hits=hits,
        clear=not hits,
        zones_checked=checked,
        notes=notes,
    )


registry.implement("geofence_check")(geofence_check)
