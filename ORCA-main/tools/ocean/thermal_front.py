"""Thermal front detection from SST.

PFZ candidates are derived from SST fronts plus chlorophyll rather than fetched
from the official INCOIS advisory, which is text and maps rather than an API.
Deriving it is what makes "explain your reasoning" answerable at all.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import GeoPoint, ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class Front(BaseModel):
    model_config = ConfigDict(extra="forbid")

    centroid: GeoPoint
    gradient_deg_c_per_km: float
    length_km: float
    orientation_deg: float | None = None


class ThermalFrontIn(ToolInput):
    bbox: tuple[float, float, float, float]
    min_gradient_deg_c_per_km: float = Field(default=0.2, gt=0.0)
    date: str | None = Field(default=None, description="ISO date. Defaults to the most recent usable composite.")


class ThermalFrontOut(ToolOutput):
    fronts: list[Front] = Field(default_factory=list)
    max_gradient_deg_c_per_km: float | None = None


register("thermal_front", description="Detect SST frontal boundaries within a bounding box.",
         input_model=ThermalFrontIn, output_model=ThermalFrontOut, agent=AgentGroup.OCEAN)


# ==========================================================================
# Implementation
# ==========================================================================
#
# Front detection is a gradient magnitude threshold plus connected-component
# clustering. Deliberately not a learned edge detector: the question "why is
# this a front" has to be answerable as "SST changes 0.31 C per km across it,
# and our threshold is 0.20", which is a sentence a judge can check.
#
# The gradient is a central difference on the cached MUR grid, in deg C per
# kilometre. Converting degrees of latitude and longitude to km separately
# matters: at 10 N a degree of longitude is 1.5% shorter than a degree of
# latitude, and a magnitude that ignored that would be biased along one axis.

import math

from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.sources.erddap import MUR_SST, GridSlice, load_cached
from tools import registry

#: Mean Earth radius, for degree -> km conversion.
_R_KM = 6371.0
_KM_PER_DEG_LAT = 2.0 * math.pi * _R_KM / 360.0  # 111.19


def _index(grid: GridSlice):
    """Index the sparse grid by integer cell so neighbour lookup is O(1).

    The cached slice is (lat, lon, value) with land and invalid cells already
    dropped, so neighbour lookup cannot assume a dense array. Keying on rounded
    integer steps rather than floats avoids equality-on-float when walking.

    Returns (cells, dlat, dlon, lat0, lon0).
    """
    lats = sorted({v[0] for v in grid.values})
    lons = sorted({v[1] for v in grid.values})
    if len(lats) < 3 or len(lons) < 3:
        return {}, 0.0, 0.0, 0.0, 0.0
    # Median spacing, not mean: one wide jump across a land gap would drag a
    # mean well off the true grid step.
    dlat = sorted(b - a for a, b in zip(lats, lats[1:]))[len(lats) // 2]
    dlon = sorted(b - a for a, b in zip(lons, lons[1:]))[len(lons) // 2]
    lat0, lon0 = lats[0], lons[0]
    cells = {
        (round((v[0] - lat0) / dlat), round((v[1] - lon0) / dlon)): v[2]
        for v in grid.values
    }
    return cells, dlat, dlon, lat0, lon0


def _gradients(grid: GridSlice):
    """Gradient magnitude and direction per cell, in deg C / km.

    Returns (i, j, magnitude, direction_deg) tuples. Only cells with all four
    neighbours present are computed. A one-sided difference at a coastline
    would report the land-sea temperature step as an ocean front, which is the
    classic false positive in this kind of detection.
    """
    cells, dlat, dlon, lat0, _lon0 = _index(grid)
    if not cells:
        return []

    out = []
    for (i, j) in cells:
        north, south = cells.get((i + 1, j)), cells.get((i - 1, j))
        east, west = cells.get((i, j + 1)), cells.get((i, j - 1))
        if north is None or south is None or east is None or west is None:
            continue

        lat = lat0 + i * dlat
        km_per_deg_lon = _KM_PER_DEG_LAT * math.cos(math.radians(lat))

        d_dy = (north - south) / (2.0 * dlat * _KM_PER_DEG_LAT)
        d_dx = (east - west) / (2.0 * dlon * km_per_deg_lon)
        magnitude = math.hypot(d_dx, d_dy)
        direction = math.degrees(math.atan2(d_dx, d_dy)) % 360.0
        out.append((i, j, magnitude, direction))
    return out


def _cluster(strong):
    """Connected components over 8-connectivity. Iterative, not recursive.

    A front spanning a few hundred cells would blow the recursion limit on a
    depth-first walk, and doing that mid-demo is avoidable.
    """
    seen = set()
    clusters = []
    steps = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for start in strong:
        if start in seen:
            continue
        stack, component = [start], []
        seen.add(start)
        while stack:
            cell = stack.pop()
            component.append(cell)
            for di, dj in steps:
                nxt = (cell[0] + di, cell[1] + dj)
                if nxt in strong and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        clusters.append(component)
    return clusters


def thermal_front(args: ThermalFrontIn) -> ThermalFrontOut:
    """SST fronts in a bounding box, from the cached MUR L4 analysis.

    Never fetches. Returns FAILED rather than an empty front list when there is
    no cached SST: "we could not look" is not "the sea is featureless", and the
    rule that governs active_alerts governs this too.
    """
    grid = load_cached(MUR_SST["dataset_id"])
    if grid is None:
        return ThermalFrontOut(
            provenance=Provenance(source=MUR_SST["dataset_id"], authority=MUR_SST["authority"]),
            status=ToolStatus.FAILED,
            error=(
                "No cached SST. Run scripts/refresh_cache.py --ocean. Tools "
                "never fetch during a query."
            ),
        )

    provenance = Provenance(
        source=grid.dataset_id,
        source_url=grid.url,
        query=grid.url,
        retrieved_at=grid.fetched_at,
        native_resolution_deg=grid.resolution_deg,
        native_units=grid.units,
        authority=grid.authority,
    )
    quality = DataQuality(
        data_age_days=round(grid.age_days, 2),
        gap_filled=grid.gap_filled,
        coverage_fraction=round(grid.coverage_fraction, 3),
        notes=[f"{grid.dataset_id} analysed_sst for {grid.time[:10]}; land cells excluded"],
    )

    gradients = _gradients(grid)
    if not gradients:
        return ThermalFrontOut(
            provenance=provenance,
            quality=quality,
            status=ToolStatus.FAILED,
            error="Cached SST grid too sparse to compute a gradient.",
        )

    _cells, dlat, dlon, lat0, lon0 = _index(grid)

    strong = {
        (i, j): (magnitude, direction)
        for i, j, magnitude, direction in gradients
        if magnitude >= args.min_gradient_deg_c_per_km
    }

    fronts = []
    for component in _cluster(strong):
        # A two-cell blob is noise in an analysis field, not a front. Three is
        # the shortest run that establishes a direction at all.
        if len(component) < 3:
            continue
        lats = [lat0 + i * dlat for i, _ in component]
        lons = [lon0 + j * dlon for _, j in component]
        magnitudes = [strong[c][0] for c in component]
        directions = [strong[c][1] for c in component]

        centroid_lat = sum(lats) / len(lats)
        centroid_lon = sum(lons) / len(lons)
        km_per_deg_lon = _KM_PER_DEG_LAT * math.cos(math.radians(centroid_lat))
        length_km = math.hypot(
            (max(lats) - min(lats)) * _KM_PER_DEG_LAT,
            (max(lons) - min(lons)) * km_per_deg_lon,
        )

        # A front runs perpendicular to its own gradient. Directions are
        # averaged on a doubled angle so that 179 deg and 1 deg -- the same
        # line -- do not average to 90.
        doubled = [math.radians(2.0 * d) for d in directions]
        mean_dir = math.degrees(
            math.atan2(
                sum(math.sin(a) for a in doubled) / len(doubled),
                sum(math.cos(a) for a in doubled) / len(doubled),
            )
        ) / 2.0
        orientation = (mean_dir + 90.0) % 180.0

        fronts.append(
            Front(
                centroid=GeoPoint(lat=round(centroid_lat, 4), lon=round(centroid_lon, 4)),
                gradient_deg_c_per_km=round(sum(magnitudes) / len(magnitudes), 4),
                length_km=round(length_km, 2),
                orientation_deg=round(orientation, 1),
            )
        )

    fronts.sort(key=lambda f: f.gradient_deg_c_per_km, reverse=True)
    return ThermalFrontOut(
        provenance=provenance,
        quality=quality,
        status=ToolStatus.DEGRADED if quality.is_degraded else ToolStatus.OK,
        fronts=fronts,
        max_gradient_deg_c_per_km=round(max(g[2] for g in gradients), 4),
    )


registry.implement("thermal_front")(thermal_front)
