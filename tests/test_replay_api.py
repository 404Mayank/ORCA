"""POST /replay/{event_id} runs an archived storm through the live tools.

No archive ships in the repo, so the trajectory test is skip-gated on
``available()`` like the other cache-dependent tests. What always runs:
unknown events 404, missing archives 409 with the fetch hint, overlapping
runs 409 on the lock, and the row-mapping rules (gust peak never mean,
breaching drivers first) against the real risk function with fixed inputs.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from core.schemas.tool_io import Provenance
from core.units import Range
from ingest.replay import EVENTS, IST
from orchestrator.main import app
from orchestrator.routes import replay as replay_route
from tools.weather.wave_forecast import WaveForecastOut
from tools.weather.wind_forecast import WindForecastOut


@pytest.fixture()
def api():
    return TestClient(app)


def test_unknown_event_is_404(api):
    response = api.post("/replay/nemo")
    assert response.status_code == 404


def test_missing_archive_is_409_with_fetch_hint(api):
    event_id = next(iter(EVENTS))
    from ingest.replay import available

    if available(EVENTS[event_id]):
        pytest.skip("archive present; the 409 path needs it missing")
    response = api.post(f"/replay/{event_id}")
    assert response.status_code == 409
    assert "scripts/replay.py" in response.json()["detail"]


def test_overlapping_runs_are_refused(api):
    acquired = replay_route._REPLAY_LOCK.acquire(blocking=False)
    assert acquired
    try:
        response = api.post("/replay/fengal")
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
    finally:
        replay_route._REPLAY_LOCK.release()


def test_lock_released_after_refusal(api):
    # Busy, then free: the missing-archive 409 afterwards proves the
    # endpoint released the lock it never ran with.
    replay_route._REPLAY_LOCK.acquire(blocking=False)
    try:
        assert api.post("/replay/fengal").status_code == 409
    finally:
        replay_route._REPLAY_LOCK.release()
    response = api.post("/replay/fengal")
    from ingest.replay import available

    if available(replay_route.EVENTS["fengal"]):
        assert response.status_code == 200
    else:
        assert response.status_code == 409
        assert "scripts/replay.py" in response.json()["detail"]


def test_chat_refuses_loudly_during_replay(api):
    """A live turn during a replay must 503, never answer from 2024."""
    replay_route._REPLAY_LOCK.acquire(blocking=False)
    try:
        response = api.post("/chat", json={"query": "hello", "session_id": "s_replay_gate"})
        assert response.status_code == 503
        assert "replay" in response.json()["detail"]
        assert response.headers.get("Retry-After") == "60"
    finally:
        replay_route._REPLAY_LOCK.release()
    assert api.post("/chat", json={"query": "hello", "session_id": "s_replay_gate2"}).status_code == 200


def test_step_hours_out_of_range_is_422(api):
    assert api.post("/replay/fengal?step_hours=0").status_code == 422
    assert api.post("/replay/fengal?step_hours=100").status_code == 422


def test_event_list_shape(api):
    body = api.get("/replay").json()
    assert isinstance(body, list)
    assert set(body) <= set(replay_route.EVENTS)


def _fixed_wave(height_max: float) -> WaveForecastOut:
    unit_m = "m"
    return WaveForecastOut(
        provenance=Provenance(source="test"),
        significant_wave_height=Range(min=height_max - 0.2, max=height_max, unit=unit_m),
        wave_period=Range(min=6.0, max=8.0, unit="s"),
        max_steepness=0.02,
    )


def _fixed_wind(gust_kn: float) -> WindForecastOut:
    return WindForecastOut(
        provenance=Provenance(source="test"),
        wind_speed=Range(min=gust_kn - 4.0, max=gust_kn - 2.0, unit="kn", peak=gust_kn),
        visibility=Range(min=10.0, max=20.0, unit="km"),
    )


def test_row_uses_gust_peak_and_names_breaching_drivers(monkeypatch):
    """Row mapping against the real risk function: the peak (not the mean)
    decides, and the why names what breached."""
    waves = _fixed_wave(4.5)
    wind = _fixed_wind(48.0)
    monkeypatch.setattr(replay_route, "wave_forecast", lambda *a, **k: waves)
    monkeypatch.setattr(replay_route, "wind_forecast", lambda *a, **k: wind)
    event = EVENTS["fengal"]
    row, breaching = replay_route._step(event, datetime(2024, 11, 30, 12, 0, tzinfo=IST), alerts_active=0)
    assert row.gust_kn == 48.0
    assert row.verdict == "no_go"
    assert "wind_speed" in row.why
    assert "wind_speed" in breaching


def test_lead_time_math_counts_back_from_landfall():
    from ingest.replay import IST

    event = EVENTS["fengal"]
    first = datetime(2024, 11, 29, 11, 30, tzinfo=IST)
    lead = (event.landfall_at - first).total_seconds() / 3600.0
    assert lead == pytest.approx(36.0)


def test_failed_layers_become_nulls_not_zeros(monkeypatch):
    """A FAILED layer is unknown, not calm: nulls, never 0.0."""
    from core.schemas.tool_io import ToolStatus

    waves = _fixed_wave(4.5)
    object.__setattr__(waves, "status", ToolStatus.FAILED)
    wind = _fixed_wind(48.0)
    object.__setattr__(wind, "status", ToolStatus.FAILED)
    monkeypatch.setattr(replay_route, "wave_forecast", lambda *a, **k: waves)
    monkeypatch.setattr(replay_route, "wind_forecast", lambda *a, **k: wind)
    event = EVENTS["fengal"]
    row, _ = replay_route._step(
        event, datetime(2024, 11, 30, 12, 0, tzinfo=IST),
        alerts_active=0,
    )
    assert row.wave_m is None and row.gust_kn is None and row.vis_km is None


def test_calm_trajectory_summarises_to_nones(monkeypatch):
    """No breach, no red: summary Nones, offset-aware landfall, warning."""
    from ingest.replay import IST

    calm = replay_route.ReplayRow(
        at=datetime(2024, 11, 28, 0, 0, tzinfo=IST).isoformat(),
        wave_m=0.5,
        gust_kn=10.0,
        vis_km=15.0,
        score=0.1,
        verdict="go",
        why="wind_speed",
    )
    monkeypatch.setattr(
        replay_route, "_step", lambda *a, **k: (calm, [])
    )
    event = EVENTS["fengal"]
    # Bypass the real cache swap: _step is stubbed, no archive is read.
    monkeypatch.setattr(replay_route, "replay_cache", lambda *a, **k: contextlib.nullcontext())
    response = replay_route._run_trajectory(event, 96)
    assert response.summary.first_no_go is None
    assert response.summary.hours_before_landfall is None
    assert response.landfall.endswith("+05:30")
    assert "process-global" in response.warning
