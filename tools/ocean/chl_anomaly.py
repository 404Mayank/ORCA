"""Chlorophyll-a concentration and its anomaly against the monthly climatology.

The anomaly is what makes causal_explain answerable: "chlorophyll is 0.4 mg/m3"
means nothing on its own, whereas "0.4 against a November mean of 1.1, which is
1.8 standard deviations low" is a testable hypothesis.
"""

from __future__ import annotations

from pydantic import Field

from core.schemas.tool_io import ToolInput, ToolOutput
from core.units import Range, Unit
from tools.registry import AgentGroup, register


class ChlAnomalyIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(default=25.0, gt=0.0)
    date: str | None = None


class ChlAnomalyOut(ToolOutput):
    concentration: Range = Field(description="mg/m3.")
    climatology_mean: float | None = None
    climatology_std: float | None = None
    anomaly_sigma: float | None = Field(default=None, description="Standard deviations from the per-pixel per-month mean.")
    month: int | None = Field(default=None, ge=1, le=12)


register("chl_anomaly", description="Chlorophyll-a concentration and its anomaly against the monthly climatology.",
         input_model=ChlAnomalyIn, output_model=ChlAnomalyOut, agent=AgentGroup.OCEAN,
         notes="Needs the reduced climatology arrays; unanswerable without them.")


# ==========================================================================
# Implementation
# ==========================================================================
#
# The anomaly, not the concentration, is the product. A concentration is a
# number; an anomaly is a claim about whether this month is unusual, and that
# is what causal_explain is built to test.
#
# Sigma is computed against a per-pixel per-month baseline built by
# ingest/climatology.py from the same VIIRS instrument family that produced
# today's value. Differencing across sensors would measure the radiometers as
# much as the water -- see that module's docstring.

import math

from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.climatology import load_month
from ingest.sources.erddap import CHL_GAPFILLED, load_cached
from tools import registry

#: Chlorophyll is log-normally distributed in the ocean -- a coastal pixel
#: swings between 0.3 and 30 mg/m3 -- so the mean and standard deviation of
#: raw concentrations are dominated by the upper tail, and a sigma computed
#: from them understates a low anomaly and overstates a high one. The
#: climatology stores raw statistics; the conversion to a symmetric scale
#: happens here, where it is visible next to the comparison it serves.
#:
#: A log-normal's parameters are recovered from its raw mean and std:
#:     sigma_ln^2 = ln(1 + (s/m)^2)
#:     mu_ln      = ln(m) - sigma_ln^2 / 2
_MIN_POSITIVE = 1e-6


def _log_sigma(value: float, mean: float, std: float) -> float | None:
    """Standard deviations from the monthly mean, on a log-normal scale."""
    if value <= _MIN_POSITIVE or mean <= _MIN_POSITIVE or std <= _MIN_POSITIVE:
        return None
    variance_ln = math.log(1.0 + (std / mean) ** 2)
    if variance_ln <= 0.0:
        return None
    sigma_ln = math.sqrt(variance_ln)
    mu_ln = math.log(mean) - variance_ln / 2.0
    return (math.log(value) - mu_ln) / sigma_ln


def chl_anomaly(args: ChlAnomalyIn) -> ChlAnomalyOut:
    """Chlorophyll and its anomaly against the per-pixel monthly baseline.

    Never fetches. Two distinct degraded states, kept distinct on purpose:

    * **No cached chlorophyll** -> FAILED. We do not know the concentration.
    * **Cached chlorophyll, no climatology** -> DEGRADED, concentration
      reported, ``anomaly_sigma`` left None. We know the value and cannot say
      whether it is unusual. Returning 0.0 sigma there would assert the
      strongest possible claim -- "exactly normal" -- from no baseline at all.
    """
    grid = load_cached(CHL_GAPFILLED["dataset_id"])
    if grid is None:
        return ChlAnomalyOut(
            provenance=Provenance(
                source=CHL_GAPFILLED["dataset_id"], authority=CHL_GAPFILLED["authority"]
            ),
            status=ToolStatus.FAILED,
            error=(
                "No cached chlorophyll. Run scripts/refresh_cache.py --ocean. "
                "Tools never fetch during a query."
            ),
            concentration=Range(min=0.0, max=0.0, unit=Unit.MILLIGRAM_PER_CUBIC_METRE),
        )

    values = grid.within_km(args.lat, args.lon, args.radius_km)
    provenance = Provenance(
        source=grid.dataset_id,
        source_url=grid.url,
        query=grid.url,
        retrieved_at=grid.fetched_at,
        native_resolution_deg=grid.resolution_deg,
        native_units=grid.units,
        authority=grid.authority,
    )

    if not values:
        return ChlAnomalyOut(
            provenance=provenance,
            status=ToolStatus.FAILED,
            error=(
                f"No valid chlorophyll pixel within {args.radius_km:.0f} km of "
                f"{args.lat:.3f},{args.lon:.3f}. The point may be over land."
            ),
            concentration=Range(min=0.0, max=0.0, unit=Unit.MILLIGRAM_PER_CUBIC_METRE),
        )

    month = int(grid.time[5:7]) if len(grid.time) >= 7 else None
    concentration = Range(min=round(min(values), 4), max=round(max(values), 4), unit=Unit.MILLIGRAM_PER_CUBIC_METRE)
    observed_mean = sum(values) / len(values)

    notes = [
        f"{grid.dataset_id} for {grid.time[:10]}; "
        f"{len(values)} pixels within {args.radius_km:.0f} km"
    ]

    climatology = load_month(month) if month else None
    # Disc-to-disc, never disc-to-pixel. See MonthlyClimatology.mean_within_km.
    baseline = (
        climatology.mean_within_km(args.lat, args.lon, args.radius_km)
        if climatology
        else None
    )

    if baseline is None:
        notes.append(
            "no monthly baseline for this pixel; anomaly not computed. "
            "Build it with: python -m ingest.climatology --month "
            f"{month or '<mm>'}"
        )
        return ChlAnomalyOut(
            provenance=provenance,
            quality=DataQuality(
                data_age_days=round(grid.age_days, 2),
                gap_filled=grid.gap_filled,
                coverage_fraction=round(grid.coverage_fraction, 3),
                notes=notes,
            ),
            status=ToolStatus.DEGRADED,
            concentration=concentration,
            month=month,
        )

    mean, std, n_pixels = baseline
    sigma = _log_sigma(observed_mean, mean, std)
    notes.append(
        f"baseline {climatology.dataset_id} month {month:02d}, "
        f"{len(climatology.years)} years "
        f"{climatology.years[0]}-{climatology.years[-1]}, "
        f"{n_pixels} baseline pixels in the same {args.radius_km:.0f} km disc"
    )

    return ChlAnomalyOut(
        provenance=provenance,
        quality=DataQuality(
            data_age_days=round(grid.age_days, 2),
            gap_filled=grid.gap_filled,
            coverage_fraction=round(grid.coverage_fraction, 3),
            notes=notes,
        ),
        status=ToolStatus.DEGRADED if grid.age_days > 3.0 else ToolStatus.OK,
        concentration=concentration,
        climatology_mean=round(mean, 4),
        climatology_std=round(std, 4),
        anomaly_sigma=round(sigma, 3) if sigma is not None else None,
        month=month,
    )


registry.implement("chl_anomaly")(chl_anomaly)
