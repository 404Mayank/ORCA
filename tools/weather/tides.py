"""Tide tool.

Source: Open-Meteo Marine `sea_level_height_msl`, verified 2026-09-04 to exist
and return 48/48 non-null hourly values in metres. A secondary source claimed
this variable was unavailable; the live call disproved it.

This is a tide SIGNAL, not a harmonic prediction from a tide table. It is good
enough to answer "is there water over the bar at dawn", which is the question
asked, and it is described that way rather than as an official tide prediction.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import ToolInput, ToolOutput
from core.units import Range, Unit
from tools.registry import AgentGroup, register


class TideExtreme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: datetime
    height_m: float
    kind: str = Field(description="'high' or 'low'.")


class TidesIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    hours: int = Field(default=24, ge=1, le=192)


class TidesOut(ToolOutput):
    tidal_range: Range = Field(description="Sea level range over the window, in m.")
    extremes: list[TideExtreme] = Field(default_factory=list)
    is_limiting: bool = Field(
        default=False,
        description="Whether tide constrains departure. When False this becomes a negative finding, not a driver.",
    )


register(
    "tides",
    description="Sea level height over the window, with high and low water times.",
    input_model=TidesIn,
    output_model=TidesOut,
    agent=AgentGroup.WEATHER,
    notes="Open-Meteo sea_level_height_msl. A signal, not an official tide table.",
)


# ==========================================================================
# Implementation
# ==========================================================================

from datetime import datetime, timedelta, timezone

from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.sources.open_meteo import (
    MARINE_NATIVE_RESOLUTION_DEG,
    CachedResponse,
    load_cached,
)
from tools import registry

IST = timezone(timedelta(hours=5, minutes=30))

#: Gravity, for deep-water wave steepness.
_G = 9.81


def _window(cached: CachedResponse, hours: int, start: datetime | None):
    """Slice the cached hourly series to the requested window.

    Returns (indices, times). Timestamps come back from Open-Meteo as local
    naive ISO strings because we request `timezone=Asia/Kolkata`, so they are
    stamped with IST here rather than being left naive -- a naive timestamp on
    this coast is a five-and-a-half hour bug.
    """
    raw_times = cached.hourly()["time"]
    times = [datetime.fromisoformat(t).replace(tzinfo=IST) for t in raw_times]
    # Floor to the hour. The series is hourly, so comparing against a wall
    # clock at 17:34 would discard the 17:00 sample -- the one describing
    # conditions right now, which is exactly the hour a departure decision is
    # made in.
    begin = (start or datetime.now(IST)).replace(minute=0, second=0, microsecond=0)
    idx = [i for i, t in enumerate(times) if t >= begin][:hours]
    if not idx:
        # The cache predates the requested window entirely. Fall back to the
        # whole series rather than returning nothing, and let data_age_days
        # carry the staleness honestly.
        idx = list(range(min(hours, len(times))))
    return idx, times


def _quality(cached: CachedResponse) -> DataQuality:
    age_days = (datetime.now(timezone.utc) - cached.fetched_at).total_seconds() / 86400.0
    return DataQuality(
        data_age_days=round(max(age_days, 0.0), 3),
        gap_filled=False,
        coverage_fraction=1.0,
        notes=[f"Open-Meteo {cached.endpoint} cache fetched {cached.fetched_at.isoformat()}"],
    )


def _provenance(cached: CachedResponse, source: str, resolution: float | None) -> Provenance:
    return Provenance(
        source=source,
        source_url=cached.url,
        query=str(cached.params),
        retrieved_at=cached.fetched_at,
        native_resolution_deg=resolution,
        authority="Open-Meteo",
    )


def tides(args: TidesIn) -> TidesOut:
    """Sea level over the window, with high and low water times.

    A tide SIGNAL from a model's sea-level field, not a harmonic prediction
    from a tide table. Good enough to answer "is there water over the bar at
    dawn", which is the question actually asked, and described that way rather
    than dressed up as an official prediction.
    """
    cached = load_cached("marine", args.lat, args.lon)
    if cached is None:
        return TidesOut(
            provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
            status=ToolStatus.FAILED,
            error=f"No cached marine data for {args.lat:.2f},{args.lon:.2f}.",
            tidal_range=Range(min=0.0, max=0.0, unit=Unit.METRE),
        )

    hourly = cached.hourly()
    idx, times = _window(cached, args.hours, None)
    levels = hourly.get("sea_level_height_msl") or []
    points = [(times[i], levels[i]) for i in idx if i < len(levels) and levels[i] is not None]

    if len(points) < 3:
        return TidesOut(
            provenance=_provenance(cached, "open_meteo_marine", MARINE_NATIVE_RESOLUTION_DEG),
            status=ToolStatus.FAILED,
            error="Too few sea-level samples to identify tidal extremes.",
            tidal_range=Range(min=0.0, max=0.0, unit=Unit.METRE),
        )

    # Local turning points on the hourly series. Cruder than a harmonic
    # analysis, but the input is hourly anyway, so a finer method would be
    # false precision.
    extremes: list[TideExtreme] = []
    for i in range(1, len(points) - 1):
        previous, current, following = points[i - 1][1], points[i][1], points[i + 1][1]
        if current > previous and current >= following:
            extremes.append(TideExtreme(time=points[i][0], height_m=current, kind="high"))
        elif current < previous and current <= following:
            extremes.append(TideExtreme(time=points[i][0], height_m=current, kind="low"))

    values = [v for _, v in points]
    return TidesOut(
        provenance=_provenance(cached, "open_meteo_marine", MARINE_NATIVE_RESOLUTION_DEG),
        quality=_quality(cached),
        tidal_range=Range(min=min(values), max=max(values), unit=Unit.METRE),
        extremes=extremes,
        # Whether the tide actually constrains departure needs the bar depth at
        # a specific landing centre, which we do not have. Left False and
        # reported as a negative finding rather than guessed.
        is_limiting=False,
    )


registry.implement("tides")(tides)
