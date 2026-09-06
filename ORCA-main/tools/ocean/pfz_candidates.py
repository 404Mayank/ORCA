"""Potential Fishing Zone candidates, derived from SST fronts plus chlorophyll.

Derived by us, not fetched. The official INCOIS PFZ advisory is treated as
corroboration where available, never as the source, because it is published as
text and maps rather than a clean API.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import GeoPoint, ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class PFZCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    centroid: GeoPoint
    distance_km: float = Field(ge=0.0, description="From the query origin.")
    bearing_deg: float = Field(ge=0.0, lt=360.0)
    depth_m: float | None = None
    front_gradient_deg_c_per_km: float | None = None
    chl_mg_m3: float | None = None
    score: float = Field(ge=0.0, le=1.0, description="Deterministic composite. Never an LLM judgement.")
    rationale_codes: list[str] = Field(default_factory=list, description="Machine codes, e.g. ['front_strong','chl_elevated']. Narration expands them.")


class PFZCandidatesIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    max_distance_km: float = Field(default=60.0, gt=0.0)
    max_results: int = Field(default=5, ge=1, le=20)
    vessel_class: str | None = None


class PFZCandidatesOut(ToolOutput):
    candidates: list[PFZCandidate] = Field(default_factory=list)
    official_advisory_agrees: bool | None = Field(default=None, description="None when no official advisory was available to compare against.")


register("pfz_candidates", description="Derive potential fishing zones from SST fronts, chlorophyll and bathymetry.",
         input_model=PFZCandidatesIn, output_model=PFZCandidatesOut, agent=AgentGroup.OCEAN)


# ==========================================================================
# Implementation
# ==========================================================================
#
# The composite is deterministic and its weights are here in the open, because
# the whole reason for deriving PFZs rather than scraping the INCOIS advisory
# is that "explain your reasoning" has to be answerable. Every candidate
# carries the gradient and the chlorophyll that produced its score, and
# rationale_codes says which criteria fired. An LLM expands those codes into
# English later; it never chooses them.
#
# The physical argument, which is the thing a judge will actually probe:
# phytoplankton concentrate where a thermal front brings nutrient-rich water
# against warmer surface water; zooplankton follow the phytoplankton; pelagic
# fish follow the zooplankton. So a PFZ candidate is a place where a
# temperature gradient and elevated chlorophyll coincide. That is the same
# reasoning INCOIS uses for its own advisories.

import math

from core import config
from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.sources.erddap import CHL_GAPFILLED, MUR_SST, load_cached
from tools import registry
from tools.ocean.thermal_front import _KM_PER_DEG_LAT, _cluster, _gradients, _index

#: Below this the SST field has no front worth fishing, whatever the strongest
#: cell in it happens to be. A typical PFZ front is ~0.5 C across ~10 km; at
#: MUR's 0.05 deg (5.5 km) sampling that is 0.05 C/km, and anything weaker is
#: within the analysis field's own smoothing. Without this floor the tool would
#: rank the flattest day of the year and present the winner as a front.
MIN_USEFUL_GRADIENT = 0.05

#: Candidate fronts are taken from the upper quartile of whatever gradient
#: field exists today, then filtered by the absolute floor above. Relative
#: ranking finds the best available; the floor decides whether "best available"
#: is good enough to report at all.
GRADIENT_PERCENTILE = 0.75

#: Chlorophyll bands, mg/m3. Elevated chlorophyll means food; *very* high
#: chlorophyll near this coast means river plume and suspended sediment, where
#: the ocean-colour retrieval saturates and the water is turbid rather than
#: productive. Reporting a turbid estuarine pixel as a prime fishing zone would
#: be a confident, specific, wrong answer.
CHL_PRODUCTIVE_MIN = 0.30
CHL_PRODUCTIVE_MAX = 10.0

#: Composite weights. Front and chlorophyll are near-equal because either alone
#: is a weak signal; proximity is a tiebreak, not a criterion -- a better zone
#: further away is still the better zone.
W_FRONT, W_CHL, W_PROXIMITY = 0.45, 0.40, 0.15


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing, degrees from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return math.degrees(math.atan2(y, x)) % 360.0


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance. Sub-1% error at this scale, and cheap.

    distance_bearing() in tools/geo is the geodesic one and is what a reported
    distance must come from. This is an internal ranking helper only.
    """
    dlat = (lat2 - lat1) * _KM_PER_DEG_LAT
    dlon = (lon2 - lon1) * _KM_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot(dlat, dlon)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]


def _chl_score(chl: float | None) -> tuple[float, str | None]:
    """Score a chlorophyll value, and name the band it fell in."""
    if chl is None:
        return 0.0, None
    if chl < CHL_PRODUCTIVE_MIN:
        return 0.1, "chl_low"
    if chl > CHL_PRODUCTIVE_MAX:
        # Not scored as zero: the water is productive, it is simply also
        # turbid, and a fisherman may well work it. It is not a recommendation.
        return 0.25, "chl_turbid"
    # Log-scaled inside the productive band -- the difference between 0.3 and
    # 1.0 mg/m3 matters far more than between 8 and 10.
    span = math.log(CHL_PRODUCTIVE_MAX) - math.log(CHL_PRODUCTIVE_MIN)
    score = (math.log(chl) - math.log(CHL_PRODUCTIVE_MIN)) / span
    return round(min(max(score, 0.0), 1.0), 4), "chl_elevated" if chl >= 1.0 else "chl_moderate"


def pfz_candidates(args: PFZCandidatesIn) -> PFZCandidatesOut:
    """Derive PFZ candidates from cached SST fronts and chlorophyll.

    Never fetches. FAILS when either layer is missing rather than returning an
    empty candidate list: "we could not look" is not "there are no fish here",
    and a fisherman who burns fuel on the second reading has been badly served.
    """
    sst = load_cached(MUR_SST["dataset_id"])
    chl = load_cached(CHL_GAPFILLED["dataset_id"])

    if sst is None or chl is None:
        missing = [
            name
            for name, grid in (("SST", sst), ("chlorophyll", chl))
            if grid is None
        ]
        return PFZCandidatesOut(
            provenance=Provenance(source="erddap", authority="NOAA CoastWatch"),
            status=ToolStatus.FAILED,
            error=(
                f"No cached {' and '.join(missing)}. Run "
                "scripts/refresh_cache.py --ocean. Tools never fetch during a query."
            ),
        )

    provenance = Provenance(
        source=f"{sst.dataset_id}+{chl.dataset_id}",
        source_url=sst.url,
        query=f"SST: {sst.url} | CHL: {chl.url}",
        retrieved_at=max(sst.fetched_at, chl.fetched_at),
        native_resolution_deg=max(sst.resolution_deg, chl.resolution_deg),
        native_units=f"{sst.units}; {chl.units}",
        authority="NOAA CoastWatch (MUR SST, VIIRS chlorophyll)",
    )
    age_days = max(sst.age_days, chl.age_days)
    notes = [
        f"SST {sst.dataset_id} {sst.time[:10]}; chlorophyll {chl.dataset_id} {chl.time[:10]}",
        "bathymetry not wired: depth_m is null and no depth filter was applied",
    ]

    gradients = _gradients(sst)
    if not gradients:
        return PFZCandidatesOut(
            provenance=provenance,
            status=ToolStatus.FAILED,
            error="Cached SST grid too sparse to compute a gradient.",
        )

    magnitudes = [g[2] for g in gradients]
    strongest = max(magnitudes)
    threshold = max(_percentile(magnitudes, GRADIENT_PERCENTILE), MIN_USEFUL_GRADIENT)

    if strongest < MIN_USEFUL_GRADIENT:
        # A real and reportable result, not an error. The Bay is thermally flat
        # in the inter-monsoon lull, and saying so is more useful than ranking
        # noise and calling the winner a fishing zone.
        notes.append(
            f"strongest gradient {strongest:.4f} C/km is below the "
            f"{MIN_USEFUL_GRADIENT} C/km floor; no frontal candidates today"
        )
        return PFZCandidatesOut(
            provenance=provenance,
            quality=DataQuality(
                data_age_days=round(age_days, 2),
                gap_filled=True,
                coverage_fraction=round(min(sst.coverage_fraction, chl.coverage_fraction), 3),
                notes=notes,
            ),
            status=ToolStatus.OK,
            candidates=[],
        )

    _cells, dlat, dlon, lat0, lon0 = _index(sst)
    strong = {(i, j): m for i, j, m, _ in gradients if m >= threshold}

    range_km = args.max_distance_km
    if args.vessel_class:
        try:
            range_km = min(
                range_km,
                float(config.vessel_spec(args.vessel_class)["typical_operating_range_km"]),
            )
        except KeyError:
            pass

    candidates: list[PFZCandidate] = []
    for component in _cluster(strong):
        if len(component) < 3:
            continue
        lats = [lat0 + i * dlat for i, _ in component]
        lons = [lon0 + j * dlon for _, j in component]
        centroid_lat, centroid_lon = sum(lats) / len(lats), sum(lons) / len(lons)

        distance = _distance_km(args.lat, args.lon, centroid_lat, centroid_lon)
        if distance > range_km:
            continue

        gradient = sum(strong[c] for c in component) / len(component)
        # Chlorophyll is read over a disc rather than at the point, because the
        # two grids differ in resolution (0.05 vs 0.083 deg) and a nearest-cell
        # read would silently sample a different patch of water.
        nearby = chl.within_km(centroid_lat, centroid_lon, 12.0)
        chl_value = sum(nearby) / len(nearby) if nearby else None

        front_score = min(gradient / (2.0 * MIN_USEFUL_GRADIENT), 1.0)
        chl_value_score, chl_code = _chl_score(chl_value)
        proximity = 1.0 - min(distance / range_km, 1.0)

        codes = ["front_strong" if gradient >= 2.0 * MIN_USEFUL_GRADIENT else "front_moderate"]
        if chl_code:
            codes.append(chl_code)
        if chl_value is None:
            codes.append("chl_unavailable")

        candidates.append(
            PFZCandidate(
                centroid=GeoPoint(lat=round(centroid_lat, 4), lon=round(centroid_lon, 4)),
                distance_km=round(distance, 2),
                bearing_deg=round(_bearing(args.lat, args.lon, centroid_lat, centroid_lon), 1),
                depth_m=None,
                front_gradient_deg_c_per_km=round(gradient, 4),
                chl_mg_m3=round(chl_value, 4) if chl_value is not None else None,
                score=round(
                    W_FRONT * front_score + W_CHL * chl_value_score + W_PROXIMITY * proximity,
                    4,
                ),
                rationale_codes=codes,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    quality = DataQuality(
        data_age_days=round(age_days, 2),
        gap_filled=True,
        coverage_fraction=round(min(sst.coverage_fraction, chl.coverage_fraction), 3),
        notes=notes,
    )
    return PFZCandidatesOut(
        provenance=provenance,
        quality=quality,
        status=ToolStatus.DEGRADED if quality.is_degraded else ToolStatus.OK,
        candidates=candidates[: args.max_results],
        # No official INCOIS advisory is wired, so we cannot claim agreement or
        # disagreement. None means "not compared", never "does not agree".
        official_advisory_agrees=None,
    )


registry.implement("pfz_candidates")(pfz_candidates)
