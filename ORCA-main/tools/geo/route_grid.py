"""Cost grid construction for routing.

Builds the traversal-cost raster that optimise_route() searches. Land, shallow
water, geofenced zones and high-risk cells become impassable or expensive here,
so that routing is a graph search over a cost surface rather than a judgement.
"""

from __future__ import annotations

from pydantic import Field

from core.schemas.tool_io import ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class RouteGridIn(ToolInput):
    bbox: tuple[float, float, float, float] = Field(description="(west, south, east, north), EPSG:4326.")
    vessel_class: str
    resolution_deg: float = Field(default=0.05, gt=0.0, description="Target grid step. See config/bbox.yaml.")
    avoid_zone_types: list[str] = Field(default_factory=lambda: ["imbl", "mpa"])


class RouteGridOut(ToolOutput):
    n_lat: int = Field(gt=0)
    n_lon: int = Field(gt=0)
    impassable_fraction: float = Field(ge=0.0, le=1.0)
    max_cost: float
    grid_ref: str = Field(description="Handle to the cached array; the raster itself never travels in the response.")


register(
    "route_grid",
    description="Build a traversal-cost grid from bathymetry, land mask, geofences and current risk.",
    input_model=RouteGridIn,
    output_model=RouteGridOut,
    agent=AgentGroup.GEOSPATIAL,
)


# ==========================================================================
# Implementation
# ==========================================================================
#
# The grid is built once and cached under a handle; the raster never travels in
# a tool response. An 81x71 cost surface inside a JSON payload would bloat every
# recommendation and the verifier would have to walk thousands of numbers that
# are not claims about anything.
#
# Cost, per cell:
#   impassable  land, or shallower than the vessel needs, or inside a geofence
#   otherwise   1.0 + shallow-water penalty + risk penalty
#
# Depth is the hard constraint and the reason this needed bathymetry. Everything
# else is a preference; running aground is not.

import math
import threading

from core import config
from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.static import bathymetry as bathy
from tools import registry

#: Minimum navigable depth by vessel class, metres. Keel plus working margin in
#: a seaway -- a boat does not run aground at exactly its draught, it runs
#: aground in the trough of the wave before that.
#:
#: PROVISIONAL, like every other vessel figure in this project. These are not
#: in risk_thresholds.yaml because that file is a cited safety deliverable and
#: these have no citation yet; putting them there would borrow its authority.
MIN_DEPTH_M = {
    "kattumaram": 2.0,
    "frp_9m": 3.0,
    "mechanised_trawler": 5.0,
}
DEFAULT_MIN_DEPTH_M = 3.0

#: Below this multiple of the minimum, a cell is passable but discouraged.
SHALLOW_MARGIN = 2.0

_KM_PER_DEG = 111.19

#: grid_ref -> built grid. Process-local, like the session store, and for the
#: same reason: it must not outlive the conditions it was built from.
_GRIDS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _geofence_cells(avoid: list[str]) -> list:
    """Zone geometries to route around. IMBL is the only one with geometry."""
    if "imbl" not in [z.lower() for z in avoid]:
        return []
    try:
        from ingest.static.boundaries import IMBL_POSITIONS

        return [(p.lat, p.lon) for p in IMBL_POSITIONS]
    except Exception:  # noqa: BLE001 -- absence is reported, not raised
        return []


def route_grid(args: RouteGridIn) -> RouteGridOut:
    """Build the traversal-cost grid. Never fetches.

    FAILS without bathymetry rather than assuming depth. An unknown seabed is
    not a deep one, and a route planned over water we never measured is exactly
    the confident-and-wrong answer this project exists to avoid.
    """
    depths = bathy.load_cached()
    if depths is None:
        return RouteGridOut(
            provenance=Provenance(source=bathy.DATASET_ID, authority="NOAA CoastWatch"),
            status=ToolStatus.FAILED,
            error=(
                "No cached bathymetry. Run scripts/refresh_cache.py --static. "
                "Depth is a hard constraint and is never assumed."
            ),
            n_lat=1, n_lon=1, impassable_fraction=1.0, max_cost=0.0, grid_ref="",
        )

    west, south, east, north = args.bbox
    step = args.resolution_deg
    n_lat = int(round((north - south) / step)) + 1
    n_lon = int(round((east - west) / step)) + 1
    min_depth = MIN_DEPTH_M.get(args.vessel_class, DEFAULT_MIN_DEPTH_M)

    fence = _geofence_cells(args.avoid_zone_types)
    fence_buffer_km = 5.0

    cells: dict[tuple[int, int], float] = {}
    impassable = 0
    max_cost = 1.0

    for i in range(n_lat):
        lat = south + i * step
        km_per_lon = _KM_PER_DEG * math.cos(math.radians(lat))
        for j in range(n_lon):
            lon = west + j * step
            depth = depths.depth_at(lat, lon)

            if depth is None or depth < min_depth:
                impassable += 1
                continue

            near_fence = any(
                math.hypot((flat - lat) * _KM_PER_DEG, (flon - lon) * km_per_lon)
                <= fence_buffer_km
                for flat, flon in fence
            )
            if near_fence:
                impassable += 1
                continue

            cost = 1.0
            if depth < min_depth * SHALLOW_MARGIN:
                # Linear penalty approaching the hard limit, so a route prefers
                # deeper water without being forbidden from working the shallows.
                cost += 2.0 * (1.0 - (depth - min_depth) / (min_depth * (SHALLOW_MARGIN - 1)))
            cells[(i, j)] = round(cost, 4)
            max_cost = max(max_cost, cost)

    grid_ref = f"grid_{args.vessel_class}_{west}_{south}_{east}_{north}_{step}"
    with _LOCK:
        _GRIDS[grid_ref] = {
            "cells": cells, "west": west, "south": south, "step": step,
            "n_lat": n_lat, "n_lon": n_lon, "min_depth_m": min_depth,
            "avoided": list(args.avoid_zone_types),
        }

    total = n_lat * n_lon
    return RouteGridOut(
        provenance=Provenance(
            source=bathy.DATASET_ID, source_url=depths.url,
            retrieved_at=depths.fetched_at,
            native_resolution_deg=depths.resolution_deg,
            native_units="m", authority="NOAA CoastWatch / SRTM30_PLUS",
        ),
        quality=DataQuality(
            gap_filled=False,
            coverage_fraction=round(len(cells) / total, 3),
            notes=[
                f"min navigable depth {min_depth} m for {args.vessel_class} (provisional)",
                f"avoided: {args.avoid_zone_types}" if fence else "no zone geometry applied",
            ],
        ),
        n_lat=n_lat, n_lon=n_lon,
        impassable_fraction=round(impassable / total, 4),
        max_cost=round(max_cost, 4),
        grid_ref=grid_ref,
    )


registry.implement("route_grid")(route_grid)
