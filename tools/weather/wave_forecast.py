"""Wave forecast tool: significant wave height, period, direction, components.

Source: Open-Meteo Marine API, verified 2026-09-04 (docs/verified_sources.md).
Native grid at our latitude is 1/12 deg (~9.3 km), measured -- NOT 0.05 deg.
`native_resolution_deg` must carry 0.0833, and regridding to the 0.05 deg
target grid is upsampling, recorded as such.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import ToolInput, ToolOutput
from core.units import Range, Unit
from tools.registry import AgentGroup, register


class WaveSample(BaseModel):
    """One hour of the wave timeseries. The trajectory block is cut from these."""

    model_config = ConfigDict(extra="forbid")

    time: datetime
    significant_height_m: float
    period_s: float | None = None
    direction_deg: float | None = None
    wind_wave_height_m: float | None = None
    swell_wave_height_m: float | None = None


class WaveForecastIn(ToolInput):
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    hours: int = Field(default=24, ge=1, le=192, description="Forecast horizon. Open-Meteo serves up to 8 days.")
    start: datetime | None = Field(default=None, description="Defaults to the next forecast hour.")


class WaveForecastOut(ToolOutput):
    significant_wave_height: Range
    wave_period: Range
    peak_direction_deg: float | None = None
    wind_wave_height: Range | None = None
    swell_wave_height: Range | None = None
    max_steepness: float | None = Field(
        default=None,
        description="Peak of S = 2*pi*Hs/(g*Tp^2) across the window. Linear wave theory; see docs/verified_sources.md.",
    )
    series: list[WaveSample] = Field(default_factory=list)


register(
    "wave_forecast",
    description="Hourly significant wave height, period, direction and wind-wave/swell split at a point.",
    input_model=WaveForecastIn,
    output_model=WaveForecastOut,
    agent=AgentGroup.WEATHER,
    safety_critical=True,
    notes="Open-Meteo Marine, 1/12 deg native. Keyless.",
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


def _steepness(hs: float, period: float) -> float | None:
    """Deep-water wave steepness, S = 2*pi*Hs / (g*T^2).

    Linear (Airy) wave theory, from the deep-water dispersion relation
    L = g*T^2/(2*pi). Textbook, not INCOIS-specific. It is what distinguishes
    a 2 m swell at 12 s, which a boat rides, from a 2 m wind sea at 5 s, which
    breaks over it -- a difference height alone cannot express, and one of the
    stated components of the INCOIS Boat Safety Index.
    """
    if not period:
        return None
    return (2.0 * 3.141592653589793 * hs) / (_G * period * period)


def wave_forecast(args: WaveForecastIn) -> WaveForecastOut:
    """Wave conditions at a point, read from the ingest cache. Never fetches."""
    cached = load_cached("marine", args.lat, args.lon)
    if cached is None:
        return WaveForecastOut(
            provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
            status=ToolStatus.FAILED,
            error=(
                f"No cached marine data for {args.lat:.2f},{args.lon:.2f}. "
                "Run ingest.sources.open_meteo.fetch_marine(). Tools never fetch "
                "during a query."
            ),
            significant_wave_height=Range(min=0.0, max=0.0, unit=Unit.METRE),
            wave_period=Range(min=0.0, max=0.0, unit=Unit.SECOND),
        )

    hourly = cached.hourly()
    idx, times = _window(cached, args.hours, args.start)

    def series(key):
        values = hourly.get(key) or []
        return [values[i] for i in idx if i < len(values) and values[i] is not None]

    hs = series("wave_height")
    periods = series("wave_period")
    if not hs:
        return WaveForecastOut(
            provenance=_provenance(cached, "open_meteo_marine", MARINE_NATIVE_RESOLUTION_DEG),
            status=ToolStatus.FAILED,
            error="Cached marine response contained no usable wave heights.",
            significant_wave_height=Range(min=0.0, max=0.0, unit=Unit.METRE),
            wave_period=Range(min=0.0, max=0.0, unit=Unit.SECOND),
        )

    samples = [
        WaveSample(
            time=times[i],
            significant_height_m=hourly["wave_height"][i],
            period_s=(hourly.get("wave_period") or [None] * len(times))[i],
            direction_deg=(hourly.get("wave_direction") or [None] * len(times))[i],
            wind_wave_height_m=(hourly.get("wind_wave_height") or [None] * len(times))[i],
            swell_wave_height_m=(hourly.get("swell_wave_height") or [None] * len(times))[i],
        )
        for i in idx
        if i < len(hourly["wave_height"]) and hourly["wave_height"][i] is not None
    ]

    steepnesses = [
        s for s in (_steepness(h, p) for h, p in zip(hs, periods)) if s is not None
    ]
    directions = series("wave_direction")
    wind_wave = series("wind_wave_height")
    swell = series("swell_wave_height")

    return WaveForecastOut(
        provenance=_provenance(cached, "open_meteo_marine", MARINE_NATIVE_RESOLUTION_DEG),
        quality=_quality(cached),
        significant_wave_height=Range(min=min(hs), max=max(hs), unit=Unit.METRE),
        wave_period=Range(
            min=min(periods) if periods else 0.0,
            max=max(periods) if periods else 0.0,
            unit=Unit.SECOND,
        ),
        peak_direction_deg=max(set(directions), key=directions.count) if directions else None,
        wind_wave_height=Range(min=min(wind_wave), max=max(wind_wave), unit=Unit.METRE) if wind_wave else None,
        swell_wave_height=Range(min=min(swell), max=max(swell), unit=Unit.METRE) if swell else None,
        max_steepness=round(max(steepnesses), 5) if steepnesses else None,
        series=samples,
    )


registry.implement("wave_forecast")(wave_forecast)
