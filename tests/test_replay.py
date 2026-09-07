"""Cyclone replay: the real pipeline against a real storm.

Skipped without a fetched archive, because these assert against measured data
rather than fixtures -- which is the point of the replay. Populate with:

    python scripts/replay.py --event fengal
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.schemas.tool_io import ToolStatus
from ingest.replay import EVENTS, IST, available, replay_cache
from ingest.sources import open_meteo
from tools.risk.risk_score import ComputeRiskScoreIn, compute_risk_score
from tools.weather.wind_forecast import WindForecastIn, wind_forecast

FENGAL = EVENTS["fengal"]


@pytest.fixture
def fengal():
    if not available(FENGAL):
        pytest.skip("no Fengal archive; run scripts/replay.py --event fengal")
    return FENGAL


def _wind(event, when):
    return wind_forecast(
        WindForecastIn(lat=event.lat, lon=event.lon, hours=6, start=when)
    )


def test_the_replay_cache_never_touches_the_live_cache(fengal):
    """A 2024 storm written into today's cache would leave the running system
    believing it was current. A historical GDACS fetch made exactly that
    mistake once; this is the same class of error, guarded."""
    live = open_meteo.CACHE_DIR
    with replay_cache(fengal):
        assert open_meteo.CACHE_DIR != live
        assert fengal.id in str(open_meteo.CACHE_DIR)
    assert open_meteo.CACHE_DIR == live, "the override must not leak"


def test_the_override_is_restored_even_if_the_body_raises(fengal):
    live = open_meteo.CACHE_DIR
    with pytest.raises(RuntimeError):
        with replay_cache(fengal):
            raise RuntimeError("boom")
    assert open_meteo.CACHE_DIR == live


def test_the_production_tools_read_the_archive_unmodified(fengal):
    """The replay is evidence only because the code under test is the code
    that answers live questions."""
    with replay_cache(fengal):
        wind = _wind(fengal, datetime(2024, 11, 30, 18, 0, tzinfo=IST))
    assert wind.status is not ToolStatus.FAILED
    assert wind.wind_speed.max > 0


def test_the_storm_breaches_the_limit_and_the_verdict_recovers(fengal):
    """The shape that makes the replay worth running: a limit crossed while the
    cyclone is over the coast, and a clean verdict once it has passed."""
    landfall = fengal.landfall_at

    with replay_cache(fengal):
        during = _wind(fengal, landfall - timedelta(hours=3))
        after = _wind(fengal, landfall + timedelta(hours=30))

    def verdict(wind, alerts):
        return compute_risk_score(
            ComputeRiskScoreIn(
                vessel_class=fengal.vessel_class,
                wind_speed=wind.wind_speed,
                alerts_active=alerts,
                alerts_checked=True,
            )
        )

    storm = verdict(during, 1)
    calm = verdict(after, 0)

    assert storm.verdict == "no_go"
    assert calm.verdict == "go", "a replay that never recovers proves nothing"
    assert storm.score > calm.score


def test_the_wind_limit_is_judged_on_the_gust_not_the_mean(fengal):
    """config/risk_thresholds.yaml applies the limit to the gust -- "gusts
    capsize boats, averages do not".

    This is worth pinning because it briefly looked like a bug: the replay
    printed the sustained mean (14 kn) beside a verdict driven by the peak
    (25.1 kn), and a correct answer read as "14 kn is over the 25 kn limit".
    """
    with replay_cache(fengal):
        wind = _wind(fengal, datetime(2024, 11, 28, 0, 0, tzinfo=IST))

    assert wind.wind_speed.peak is not None
    assert wind.wind_speed.peak > wind.wind_speed.max, "the gust exceeds the mean"

    result = compute_risk_score(
        ComputeRiskScoreIn(vessel_class="frp_9m", wind_speed=wind.wind_speed)
    )
    evaluation = next(
        c.evaluation for c in result.contributions if c.driver_id == "wind_speed"
    )
    # The mean is comfortably inside the limit; the gust is not, and the gust
    # is what decides.
    assert wind.wind_speed.max < evaluation.threshold.value
    assert evaluation.breaching


def test_gaja_is_declared_as_wind_only():
    """Open-Meteo's marine archive does not reach 2018. Gaja is kept because it
    is the storm this coast remembers, and its note says what it lacks rather
    than the replay quietly running a driver short."""
    gaja = EVENTS["gaja"]
    assert "WIND ONLY" in gaja.note
    assert gaja.start.startswith("2018")


def test_every_event_has_a_landfall_inside_its_own_window():
    for event in EVENTS.values():
        start = datetime.fromisoformat(event.start).replace(tzinfo=IST)
        end = datetime.fromisoformat(event.end).replace(tzinfo=IST)
        assert start <= event.landfall_at <= end + timedelta(days=1), event.id
