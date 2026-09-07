"""POST /replay/{event_id} -- a real archived cyclone through the live tools.

From CLAUDE.md: a live demo on a calm day proves nothing. Every live answer
is `go` in September, so this is the only way to watch the risk score go red:
archived observations are read through the production tools
(``wave_forecast``, ``wind_forecast``, ``compute_risk_score``), unmodified,
exactly like ``scripts/replay.py`` does. The row-building below mirrors that
script field for field (gust peak, not mean; breaching drivers first), so the
CLI table and this endpoint cannot disagree.

Honesty constraints, all load-bearing:

* Button-per-event, never a toggle. There is no half-state: the archive is
  swapped in for the run and restored in a ``finally``.
* The swap is process-global (``replay_cache`` repoints ``CACHE_DIR``), so
  overlapping runs are refused with 409 and live /chat traffic during a run
  is unsupported -- stated here, in the response, and in PROGRESS.md.
* Archives are fetched by hand (``scripts/replay.py --fetch`` path); this
  endpoint never fetches. A missing archive is 409 with the fetch hint, not
  an empty trajectory.
* Every row is labelled replay data with the real authority (GDACS/IMD),
  never live conditions.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import ToolStatus
from ingest.replay import EVENTS, IST, ReplayEvent, available, replay_cache
from tools.risk.risk_score import ComputeRiskScoreIn, compute_risk_score
from tools.weather.wave_forecast import WaveForecastIn, wave_forecast
from tools.weather.wind_forecast import WindForecastIn, wind_forecast

router = APIRouter(tags=["replay"])

#: One replay at a time: the cache swap is process-global.
_REPLAY_LOCK = threading.Lock()


def replay_in_progress() -> bool:
    """Whether a replay currently owns the process-global cache."""
    return _REPLAY_LOCK.locked()


class ReplayRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str = Field(description="Window start, ISO IST. Archived, not live.")
    wave_m: float | None = Field(default=None, description="Significant height max, or null when the layer failed.")
    gust_kn: float | None = Field(default=None, description="Peak gust (never the mean), or null.")
    vis_km: float | None = Field(default=None, description="Visibility min, or null.")
    score: float
    # Plain str, not a Literal: the tool's contract is a string and a new
    # band must degrade this row, never 500 the whole trajectory.
    verdict: str = Field(description="go | marginal | no_go today; anything else is shown, not crashed on.")
    why: str = Field(description="Breaching drivers first, else the downgrade reason.")


class ReplaySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_no_go: str | None = None
    hours_before_landfall: float | None = None
    first_breach: str | None = None


class ReplayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    name: str
    place: str
    vessel_class: str
    landfall: str
    authority: str
    note: str
    replay: Literal[True] = True
    warning: str = Field(
        default=(
            "Archived replay data, not live conditions. Do not run with "
            "live /chat traffic: the cache swap is process-global and "
            "single-worker."
        ),
        description="The single-operator constraint, in the payload itself.",
    )
    rows: list[ReplayRow]
    summary: ReplaySummary


def _step(event: ReplayEvent, when: datetime, alerts_active: int) -> tuple[ReplayRow, list[str]]:
    """One window through the production tools. Mirrors scripts/replay.py.

    Returns the row plus the breaching driver list separately: summary
    facts derive from the list, never by re-parsing the display string.
    """
    waves = wave_forecast(WaveForecastIn(lat=event.lat, lon=event.lon, hours=6, start=when))
    wind = wind_forecast(WindForecastIn(lat=event.lat, lon=event.lon, hours=6, start=when))
    risk = compute_risk_score(
        ComputeRiskScoreIn(
            vessel_class=event.vessel_class,
            wave_height=waves.significant_wave_height if waves.status is not ToolStatus.FAILED else None,
            wave_steepness=waves.max_steepness if waves.status is not ToolStatus.FAILED else None,
            wind_speed=wind.wind_speed if wind.status is not ToolStatus.FAILED else None,
            visibility=wind.visibility if wind.status is not ToolStatus.FAILED else None,
            alerts_active=alerts_active,
            alerts_checked=True,
        )
    )
    breaching = [c.driver_id for c in risk.contributions if c.evaluation.breaching]
    if breaching:
        why = f"{', '.join(breaching)} over limit"
    elif risk.downgrade_reason:
        why = risk.downgrade_reason
    else:
        why = risk.limiting_driver or ""
    gust = wind.wind_speed.peak or wind.wind_speed.max
    row = ReplayRow(
        at=when.isoformat(),
        wave_m=round(waves.significant_wave_height.max, 2) if waves.status is not ToolStatus.FAILED else None,
        gust_kn=round(gust, 1) if wind.status is not ToolStatus.FAILED else None,
        vis_km=round(wind.visibility.min, 1)
        if wind.status is not ToolStatus.FAILED and wind.visibility
        else None,
        score=round(risk.score, 3),
        verdict=risk.verdict,
        why=why,
    )
    return row, breaching


def _run_trajectory(event: ReplayEvent, step_hours: int) -> ReplayResponse:
    start = datetime.fromisoformat(event.start).replace(tzinfo=IST)
    end = datetime.fromisoformat(event.end).replace(tzinfo=IST)
    landfall = event.landfall_at
    rows: list[ReplayRow] = []
    first_no_go: datetime | None = None
    first_breach: datetime | None = None
    when = start
    with replay_cache(event):
        while when <= end:
            # The advisory is in force for the 48 h before landfall, roughly
            # when IMD issues for a Bay system. Same rule as the CLI replay.
            alerts = 1 if landfall - timedelta(hours=48) <= when <= landfall + timedelta(hours=12) else 0
            row, breaching = _step(event, when, alerts)
            rows.append(row)
            if row.verdict == "no_go" and first_no_go is None:
                first_no_go = when
            if breaching and first_breach is None:
                first_breach = when
            when += timedelta(hours=step_hours)
    return ReplayResponse(
        event=event.id,
        name=event.name,
        place=event.place,
        vessel_class=event.vessel_class,
        landfall=event.landfall_at.isoformat(),
        authority=event.authority,
        note=event.note,
        rows=rows,
        summary=ReplaySummary(
            first_no_go=first_no_go.isoformat() if first_no_go else None,
            hours_before_landfall=round((landfall - first_no_go).total_seconds() / 3600, 1)
            if first_no_go
            else None,
            first_breach=first_breach.isoformat() if first_breach else None,
        ),
    )


@router.get("/replay", response_model=list[str])
def replay_events() -> list[str]:
    """Which archived storms have a usable cache on disk."""
    return sorted(eid for eid, event in EVENTS.items() if available(event))


@router.post("/replay/{event_id}", response_model=ReplayResponse)
def replay_event(event_id: str, step_hours: int = Query(default=6, ge=1, le=48)) -> Any:
    """Run one archived storm end to end. One at a time; live traffic waits.

    Out-of-range steps 422 rather than silently clamping: a caller asking
    for 0-hour steps has a bug, and hiding it would corrupt the lead-time
    story this endpoint exists to tell.
    """
    event = EVENTS.get(event_id)
    if event is None:
        return JSONResponse(
            status_code=404, content={"detail": f"unknown replay event {event_id!r}"}
        )
    # Busy before missing: a held lock means another run owns the cache
    # state, whatever is or isn't on disk.
    if not _REPLAY_LOCK.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"detail": "a replay is already running"})
    try:
        if not available(event):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        f"archive for {event_id!r} is not on disk; fetch it first "
                        f"with: python scripts/replay.py --event {event_id} "
                        f"(fetches the archive on first run)"
                    )
                },
            )
        return _run_trajectory(event, step_hours)
    finally:
        _REPLAY_LOCK.release()
