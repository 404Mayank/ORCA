"""Wind forecast tool: sustained speed, gusts, direction, visibility.

Source: Open-Meteo Forecast API, verified 2026-09-04. `wind_speed_unit=kn` is
honoured by the API, so knots are requested rather than hand-converted.

Unit trap recorded in docs/verified_sources.md: `visibility` comes back in
METRES. Our threshold is in km. Convert at the ingest boundary, never at
comparison time -- core.units.evaluate() raises on a unit mismatch by design.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import ToolInput, ToolOutput
from core.units import Range, Unit
from tools.registry import AgentGroup, register


class WindSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: datetime
    speed_kn: float
    gust_kn: float | None = None
    direction_deg: float | None = None


class WindForecastIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    hours: int = Field(default=24, ge=1, le=192)
    start: datetime | None = None


class WindForecastOut(ToolOutput):
    wind_speed: Range = Field(description="Sustained speed. `peak` carries the gust, which is what the risk function thresholds.")
    direction_deg: float | None = None
    visibility: Range | None = Field(default=None, description="In km. The API returns metres; converted at ingest.")
    series: list[WindSample] = Field(default_factory=list)


register(
    "wind_forecast",
    description="Hourly sustained wind, gusts, direction and visibility at a point.",
    input_model=WindForecastIn,
    output_model=WindForecastOut,
    agent=AgentGroup.WEATHER,
    safety_critical=True,
    notes="Open-Meteo Forecast. Different grid from the marine endpoint; co-located only after regridding.",
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


#: The API returns visibility in METRES. Our threshold is in km. Converted here,
#: at the boundary, where it is visible in a diff -- see docs/verified_sources.md.
_M_PER_KM = 1000.0


def wind_forecast(args: WindForecastIn) -> WindForecastOut:
    """Wind, gusts and visibility at a point, from the ingest cache."""
    cached = load_cached("forecast", args.lat, args.lon)
    if cached is None:
        return WindForecastOut(
            provenance=Provenance(source="open_meteo_forecast", authority="Open-Meteo"),
            status=ToolStatus.FAILED,
            error=(
                f"No cached forecast data for {args.lat:.2f},{args.lon:.2f}. "
                "Run ingest.sources.open_meteo.fetch_forecast()."
            ),
            wind_speed=Range(min=0.0, max=0.0, unit=Unit.KNOT),
        )

    hourly = cached.hourly()
    idx, times = _window(cached, args.hours, args.start)

    def series(key):
        values = hourly.get(key) or []
        return [values[i] for i in idx if i < len(values) and values[i] is not None]

    speeds = series("wind_speed_10m")
    gusts = series("wind_gusts_10m")
    if not speeds:
        return WindForecastOut(
            provenance=_provenance(cached, "open_meteo_forecast", None),
            status=ToolStatus.FAILED,
            error="Cached forecast response contained no usable wind speeds.",
            wind_speed=Range(min=0.0, max=0.0, unit=Unit.KNOT),
        )

    # The gust goes in `peak`, not in the qualifier text, because the risk
    # function thresholds it. Gusts capsize boats; averages do not.
    peak = max(gusts) if gusts else None
    qualifier = f"gusting {peak:.0f}" if peak is not None else None

    visibility_m = series("visibility")
    visibility = (
        Range(
            min=round(min(visibility_m) / _M_PER_KM, 2),
            max=round(max(visibility_m) / _M_PER_KM, 2),
            unit=Unit.KILOMETRE,
        )
        if visibility_m
        else None
    )

    directions = series("wind_direction_10m")
    samples = [
        WindSample(
            time=times[i],
            speed_kn=hourly["wind_speed_10m"][i],
            gust_kn=(hourly.get("wind_gusts_10m") or [None] * len(times))[i],
            direction_deg=(hourly.get("wind_direction_10m") or [None] * len(times))[i],
        )
        for i in idx
        if i < len(hourly["wind_speed_10m"]) and hourly["wind_speed_10m"][i] is not None
    ]

    return WindForecastOut(
        provenance=_provenance(cached, "open_meteo_forecast", None),
        quality=_quality(cached),
        wind_speed=Range(
            min=min(speeds),
            max=max(speeds),
            unit=Unit.KNOT,
            qualifier=qualifier,
            peak=peak if peak is not None and peak >= max(speeds) else None,
        ),
        direction_deg=round(sum(directions) / len(directions), 1) if directions else None,
        visibility=visibility,
        series=samples,
    )


registry.implement("wind_forecast")(wind_forecast)
