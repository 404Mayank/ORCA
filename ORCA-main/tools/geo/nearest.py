"""Place resolution and nearest-point geometry.

The LLM may say the word "Nagapattinam". It may never produce the coordinate.
That is what these tools are for, and it is why SpatialReference.resolved_by
exists: a lat/lon with no resolving tool_call_id means a coordinate was
invented, and the verifier rejects it.
"""

from __future__ import annotations

from pydantic import Field

from core.schemas.tool_io import ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class ResolvePlaceIn(ToolInput):
    name: str = Field(min_length=1, description="Place as the user said it.")


class ResolvePlaceOut(ToolOutput):
    matched_name: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    in_bbox: bool = Field(description="Inside the South Coromandel box. False drives an out_of_region refusal.")
    match_confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class NearestLandingCentreIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    max_results: int = Field(default=1, ge=1, le=10)


class NearestLandingCentreOut(ToolOutput):
    name: str
    lat: float
    lon: float
    distance_km: float = Field(ge=0.0)
    bearing_deg: float = Field(ge=0.0, lt=360.0)
    district: str | None = None


class DistanceBearingIn(ToolInput):
    from_lat: float = Field(ge=-90.0, le=90.0)
    from_lon: float = Field(ge=-180.0, le=180.0)
    to_lat: float = Field(ge=-90.0, le=90.0)
    to_lon: float = Field(ge=-180.0, le=180.0)


class DistanceBearingOut(ToolOutput):
    distance_km: float = Field(ge=0.0)
    bearing_deg: float = Field(ge=0.0, lt=360.0)
    method: str = Field(default="geodesic_wgs84", description="Recorded so a great-circle approximation is never mistaken for a geodesic.")


register("resolve_place", description="Resolve a place name to a coordinate against the landing-centre table.",
         input_model=ResolvePlaceIn, output_model=ResolvePlaceOut, agent=AgentGroup.GEOSPATIAL,
         notes="The only legitimate way a coordinate enters the system from a place name.")
register("nearest_landing_centre", description="Nearest fish landing centre to a point, with distance and bearing.",
         input_model=NearestLandingCentreIn, output_model=NearestLandingCentreOut, agent=AgentGroup.GEOSPATIAL)
register("distance_bearing", description="Geodesic distance and initial bearing between two points.",
         input_model=DistanceBearingIn, output_model=DistanceBearingOut, agent=AgentGroup.GEOSPATIAL)


# ==========================================================================
# Implementation
# ==========================================================================

from functools import lru_cache

from pyproj import Geod

from core import config
from core.schemas.tool_io import Provenance, ToolStatus
from tools import registry

#: WGS84 geodesic. Distances are true geodesic distances on the ellipsoid, not
#: great-circle approximations on a sphere -- the difference is small at these
#: ranges but the field `DistanceBearingOut.method` claims geodesic, so it had
#: better be one.
_GEOD = Geod(ellps="WGS84")


@lru_cache(maxsize=1)
def _gazetteer() -> dict[str, dict]:
    """Place name -> coordinate.

    PROVISIONAL. Seeded from `config/bbox.yaml` reference_points, which are
    approximate and explicitly marked as such in that file. The real source is
    the INCOIS fish landing centre set, loaded by
    ingest/static/landing_centres.py, which is not yet obtained.

    Every result therefore carries `match_confidence` below 1.0 and a
    provenance source of "bbox.yaml#reference_points" rather than
    "landing_centres", so nothing downstream can mistake a demo seed for the
    official register.
    """
    points = config.load_yaml("bbox.yaml")["reference_points"]
    return {
        name.lower().replace("_", " "): {
            "name": name.replace("_", " ").title(),
            "lat": float(spec["lat"]),
            "lon": float(spec["lon"]),
            "note": spec.get("note"),
        }
        for name, spec in points.items()
    }


_PROVISIONAL_GAZETTEER = Provenance(
    source="bbox.yaml#reference_points",
    native_units="degrees",
    authority="ORCA (provisional)",
)


def resolve_place(args: ResolvePlaceIn) -> ResolvePlaceOut:
    """Resolve a place name to a coordinate.

    The only legitimate way a coordinate enters the system from a name. An LLM
    may produce the string "Nagapattinam"; it may never produce 10.77, 79.84.
    """
    key = args.name.strip().lower()
    table = _gazetteer()

    hit = table.get(key)
    confidence = 0.8  # provisional gazetteer, never a clean 1.0
    if hit is None:
        # Substring fallback, so "Nagapattinam port" still resolves.
        candidates = [v for k, v in table.items() if key in k or k in key]
        if len(candidates) == 1:
            hit = candidates[0]
            confidence = 0.6
    if hit is None:
        return ResolvePlaceOut(
            provenance=_PROVISIONAL_GAZETTEER,
            status=ToolStatus.FAILED,
            error=(
                f"No place matching {args.name!r} in the provisional gazetteer "
                f"({sorted(table)}). The INCOIS landing centre set is not yet "
                "ingested."
            ),
            matched_name=args.name,
            lat=0.0,
            lon=0.0,
            in_bbox=False,
            match_confidence=0.0,
        )

    return ResolvePlaceOut(
        provenance=_PROVISIONAL_GAZETTEER,
        matched_name=hit["name"],
        lat=hit["lat"],
        lon=hit["lon"],
        in_bbox=config.in_bbox(hit["lat"], hit["lon"]),
        match_confidence=confidence,
    )


def distance_bearing(args: DistanceBearingIn) -> DistanceBearingOut:
    """Geodesic distance and initial bearing between two points."""
    azimuth, _, metres = _GEOD.inv(
        args.from_lon, args.from_lat, args.to_lon, args.to_lat
    )
    return DistanceBearingOut(
        provenance=Provenance(source="deterministic", authority="ORCA"),
        distance_km=round(metres / 1000.0, 3),
        bearing_deg=round(azimuth % 360.0, 1),
        method="geodesic_wgs84",
    )


def nearest_landing_centre(args: NearestLandingCentreIn) -> NearestLandingCentreOut:
    """Nearest landing centre to a point, by geodesic distance.

    Used to work out how far a boat is from shelter, which is what makes
    Window.turn_back computable rather than a flat guess.
    """
    table = _gazetteer()
    best = None
    for entry in table.values():
        azimuth, _, metres = _GEOD.inv(args.lon, args.lat, entry["lon"], entry["lat"])
        if best is None or metres < best[1]:
            best = (entry, metres, azimuth)

    entry, metres, azimuth = best
    return NearestLandingCentreOut(
        provenance=_PROVISIONAL_GAZETTEER,
        name=entry["name"],
        lat=entry["lat"],
        lon=entry["lon"],
        distance_km=round(metres / 1000.0, 3),
        bearing_deg=round(azimuth % 360.0, 1),
    )


registry.implement("resolve_place")(resolve_place)
registry.implement("distance_bearing")(distance_bearing)
registry.implement("nearest_landing_centre")(nearest_landing_centre)
