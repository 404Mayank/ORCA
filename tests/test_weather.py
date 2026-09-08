"""Tests for the weather tools and the Open-Meteo ingest cache.

No test here touches the network. Each builds a cache file with known values
and reads it back, so the suite stays deterministic and runs at a venue with no
wifi -- which is the same reason the tools read a cache rather than fetching.

The most important test in the file is
``test_active_alerts_fails_rather_than_reporting_no_alerts``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core.units import Unit
from ingest.sources import gdacs, open_meteo
from tools.risk.risk_score import ComputeRiskScoreIn, compute_risk_score
from tools.weather.alerts import ActiveAlertsIn, active_alerts
from tools.weather.tides import TidesIn, tides
from tools.weather.wave_forecast import WaveForecastIn, wave_forecast
from tools.weather.wind_forecast import WindForecastIn, wind_forecast

IST = timezone(timedelta(hours=5, minutes=30))
LAT, LON = 10.77, 79.84


def _times(n: int, start: datetime) -> list[str]:
    return [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(open_meteo, "CACHE_DIR", tmp_path)
    return tmp_path


def _write(cache_dir, endpoint: str, hourly: dict, *, age_hours: float = 0.0):
    start = datetime.now(IST).replace(minute=0, second=0, microsecond=0)
    n = len(next(iter(hourly.values())))
    payload = {
        "latitude": 10.79166,
        "longitude": 79.95836,
        "hourly": {"time": _times(n, start), **hourly},
        "hourly_units": {"wave_height": "m", "visibility": "m"},
    }
    record = {
        "endpoint": endpoint,
        "url": "https://example.invalid/test",
        "params": {"latitude": LAT, "longitude": LON},
        "fetched_at": (
            datetime.now(timezone.utc) - timedelta(hours=age_hours)
        ).isoformat(),
        "payload": payload,
    }
    path = cache_dir / f"{endpoint}_{LAT:.2f}_{LON:.2f}_20260904.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# ==========================================================================
# The rule that matters most
# ==========================================================================


def test_active_alerts_fails_rather_than_reporting_no_alerts(no_alert_sources):
    """'We could not check' must never be reported as 'there is nothing there'.

    If this returned count=0 when no source was reachable, a system with no
    cyclone feed at all could tell a fisherman there is no cyclone -- the
    single most dangerous thing this project could do.

    The outage is created by a fixture rather than borrowed from reality. Until
    2026-09-06 there was no alert source and this test passed by accident; when
    GDACS was wired it began asserting the opposite of the truth.
    """
    out = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON))
    assert out.status.value == "failed"
    assert out.error is not None
    assert out.checked is False
    assert out.checked_types == [], "an unchecked type must not appear as checked"


def test_active_alerts_reports_an_empty_list_when_it_really_did_check():
    """The other half, and the one that makes a `go` verdict reachable.

    GDACS reachable and nothing in force is a *citable negative finding*, not a
    gap. count=0 with checked=True is what licenses "no cyclone is active in
    the Bay of Bengal" as a sentence in the answer.
    """
    out = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON, types=["cyclone"]))
    if out.status.value == "failed":
        pytest.skip("no GDACS cache present; run scripts/refresh_cache.py --alerts")
    assert out.checked is True
    assert "cyclone" in out.checked_types


def test_a_type_with_no_source_is_never_reported_as_clear():
    """Lightning has no feed. Asking for it must not come back 'all clear'."""
    out = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON, types=["cyclone", "lightning"]))
    assert "lightning" not in out.checked_types
    if out.status.value != "failed":
        # Partial coverage is a degraded check, never a clean one.
        assert out.status.value == "degraded"


def test_an_advisory_in_force_outranks_the_arithmetic(one_cyclone_in_force):
    """An authority's warning beats our own threshold maths, every time."""
    from core.units import Range

    alerts = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON, types=["cyclone"]))
    assert alerts.count == 1
    assert alerts.alerts[0].authority == "GDACS"

    out = compute_risk_score(
        ComputeRiskScoreIn(
            vessel_class="frp_9m",
            wave_height=Range(min=0.3, max=0.44, unit=Unit.METRE),
            wind_speed=Range(min=1.2, max=9.4, unit=Unit.KNOT),
            alerts_checked=True,
            alerts_active=alerts.count,
        )
    )
    assert out.verdict == "no_go"
    assert "advisor" in out.downgrade_reason


def test_a_failed_alert_check_forces_no_go_even_in_flat_calm(no_alert_sources):
    """The whole safety chain, end to end.

    Glass-flat sea, no wind: the arithmetic says go. The alert check failed,
    so the verdict is no_go anyway, and the reason names the alert check rather
    than the sea state -- because that is what a fisherman needs to hear.
    """
    from core.units import Range

    alerts = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON))
    out = compute_risk_score(
        ComputeRiskScoreIn(
            vessel_class="frp_9m",
            wave_height=Range(min=0.3, max=0.44, unit=Unit.METRE),
            wind_speed=Range(min=1.2, max=9.4, unit=Unit.KNOT),
            alerts_checked=(alerts.status.value == "ok"),
            alerts_active=alerts.count,
        )
    )
    assert out.score < 0.35, "the arithmetic alone would say go"
    assert out.verdict == "no_go"
    assert "alert check" in out.downgrade_reason


# ==========================================================================
# Tools never fetch
# ==========================================================================


@pytest.mark.parametrize(
    "tool,args",
    [
        (wave_forecast, WaveForecastIn(lat=LAT, lon=LON)),
        (wind_forecast, WindForecastIn(lat=LAT, lon=LON)),
        (tides, TidesIn(lat=LAT, lon=LON)),
    ],
)
def test_a_missing_cache_fails_loudly_instead_of_fetching(cache_dir, tool, args):
    """A query must never reach the network. An empty cache is a failure."""
    out = tool(args)
    assert out.status.value == "failed"
    assert "cach" in out.error.lower()


# ==========================================================================
# wave_forecast
# ==========================================================================


def test_wave_forecast_reads_the_cache(cache_dir):
    _write(
        cache_dir,
        "marine",
        {
            "wave_height": [1.8, 2.0, 2.2, 2.1],
            "wave_period": [7.0, 7.2, 7.4, 7.1],
            "wave_direction": [45, 45, 50, 45],
            "wind_wave_height": [0.9, 1.0, 1.1, 1.0],
            "swell_wave_height": [1.5, 1.6, 1.7, 1.6],
        },
    )
    out = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=4))
    assert out.status.value == "ok"
    assert out.significant_wave_height.min == 1.8
    assert out.significant_wave_height.max == 2.2
    assert out.significant_wave_height.unit is Unit.METRE
    assert out.peak_direction_deg == 45
    assert len(out.series) == 4


def test_wave_forecast_reports_the_true_native_resolution(cache_dir):
    """0.0833, not 0.05. The target grid is a common frame, not a resolution."""
    _write(cache_dir, "marine", {"wave_height": [1.0, 1.1], "wave_period": [6.0, 6.0]})
    out = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=2))
    assert out.provenance.native_resolution_deg == pytest.approx(1 / 12, abs=1e-4)


def test_wave_steepness_matches_linear_theory(cache_dir):
    """S = 2*pi*Hs / (g*T^2), from the deep-water dispersion relation."""
    _write(cache_dir, "marine", {"wave_height": [2.0, 2.0], "wave_period": [7.0, 7.0]})
    out = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=2))
    expected = (2 * 3.141592653589793 * 2.0) / (9.81 * 49.0)
    assert out.max_steepness == pytest.approx(expected, abs=1e-5)


def test_steepness_distinguishes_swell_from_wind_sea(cache_dir):
    """The reason steepness is computed at all.

    Same 2 m height, different period. The short-period sea is far steeper --
    a difference height alone cannot express, and one a boat feels acutely.
    """
    _write(cache_dir, "marine", {"wave_height": [2.0, 2.0], "wave_period": [12.0, 12.0]})
    swell = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=2)).max_steepness

    _write(cache_dir, "marine", {"wave_height": [2.0, 2.0], "wave_period": [5.0, 5.0]})
    wind_sea = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=2)).max_steepness

    assert wind_sea > swell * 4


def test_stale_cache_is_reported_not_hidden(cache_dir):
    """Never silently substitute stale data for fresh."""
    _write(
        cache_dir,
        "marine",
        {"wave_height": [1.0, 1.1], "wave_period": [6.0, 6.0]},
        age_hours=72,
    )
    out = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=2))
    assert out.quality.data_age_days == pytest.approx(3.0, abs=0.1)
    assert out.quality.is_degraded


# ==========================================================================
# wind_forecast
# ==========================================================================


def test_visibility_is_converted_from_metres_to_km(cache_dir):
    """A verified unit trap: the API returns metres, our threshold is in km.

    Converted at the ingest boundary. If this regressed, a 24 km visibility
    would be compared against a 2 km floor as though it were 24000 km -- or
    worse, a 1.5 km fog would read as safe.
    """
    _write(
        cache_dir,
        "forecast",
        {
            "wind_speed_10m": [12.0, 14.0],
            "wind_gusts_10m": [18.0, 20.0],
            "wind_direction_10m": [45, 50],
            "visibility": [24940.0, 22060.0],
        },
    )
    out = wind_forecast(WindForecastIn(lat=LAT, lon=LON, hours=2))
    assert out.visibility.unit is Unit.KILOMETRE
    assert out.visibility.max == pytest.approx(24.94, abs=0.01)
    assert out.visibility.min == pytest.approx(22.06, abs=0.01)


def test_the_gust_lands_in_peak_where_the_risk_function_reads_it(cache_dir):
    """Gusts capsize boats; averages do not. Range.peak must carry the gust."""
    _write(
        cache_dir,
        "forecast",
        {
            "wind_speed_10m": [15.0, 20.0],
            "wind_gusts_10m": [25.0, 32.0],
            "wind_direction_10m": [45, 45],
            "visibility": [20000.0, 20000.0],
        },
    )
    out = wind_forecast(WindForecastIn(lat=LAT, lon=LON, hours=2))
    assert out.wind_speed.peak == 32.0
    assert out.wind_speed.worst_case == 32.0
    assert "gusting" in out.wind_speed.qualifier


def test_wind_speed_is_already_in_knots(cache_dir):
    """wind_speed_unit=kn is requested, so the API converts and we do not."""
    _write(
        cache_dir,
        "forecast",
        {"wind_speed_10m": [15.0, 20.0], "wind_gusts_10m": [25.0, 25.0]},
    )
    out = wind_forecast(WindForecastIn(lat=LAT, lon=LON, hours=2))
    assert out.wind_speed.unit is Unit.KNOT
    assert out.wind_speed.max == 20.0


# ==========================================================================
# tides
# ==========================================================================


def test_tides_finds_high_and_low_water(cache_dir):
    _write(
        cache_dir,
        "marine",
        {
            "wave_height": [1.0] * 7,
            "wave_period": [6.0] * 7,
            "sea_level_height_msl": [0.2, 0.4, 0.66, 0.5, 0.3, 0.19, 0.35],
        },
    )
    out = tides(TidesIn(lat=LAT, lon=LON, hours=7))
    assert out.status.value == "ok"
    kinds = [e.kind for e in out.extremes]
    assert "high" in kinds and "low" in kinds
    assert out.tidal_range.max == 0.66
    assert out.tidal_range.min == 0.19


def test_tide_is_not_claimed_to_be_limiting_without_a_bar_depth(cache_dir):
    """We do not have bar depths per landing centre, so we do not guess.

    Reported as a negative finding -- 'tide is not the constraint' -- rather
    than as a driver we cannot actually evaluate.
    """
    _write(
        cache_dir,
        "marine",
        {
            "wave_height": [1.0] * 5,
            "wave_period": [6.0] * 5,
            "sea_level_height_msl": [0.2, 0.5, 0.7, 0.4, 0.2],
        },
    )
    assert tides(TidesIn(lat=LAT, lon=LON, hours=5)).is_limiting is False


# ==========================================================================
# Cache mechanics
# ==========================================================================


def test_cache_records_the_snapped_grid_cell_not_the_requested_point(cache_dir):
    """Asking for 79.84 and being served 79.95836 is a 12 km difference.

    Provenance carries what the model actually served, so we never overstate
    our spatial precision.
    """
    _write(cache_dir, "marine", {"wave_height": [1.0], "wave_period": [6.0]})
    cached = open_meteo.load_cached("marine", LAT, LON)
    assert cached.snapped_lon == pytest.approx(79.95836, abs=1e-4)
    assert cached.snapped_lon != LON


def test_cache_path_is_deterministic():
    a = open_meteo.cache_path("marine", 10.77, 79.84, "20260904")
    b = open_meteo.cache_path("marine", 10.771, 79.844, "20260904")
    assert a == b, "coordinates finer than the grid must not split the cache"


def test_provenance_carries_the_exact_query(cache_dir):
    """Every external fetch records the exact request, so a number can be
    reproduced months later when someone asks where it came from."""
    _write(cache_dir, "marine", {"wave_height": [1.0], "wave_period": [6.0]})
    out = wave_forecast(WaveForecastIn(lat=LAT, lon=LON, hours=1))
    assert out.provenance.query is not None
    assert out.provenance.source_url is not None
    assert out.provenance.retrieved_at is not None


# ==========================================================================
# Derived wave advisories
# ==========================================================================


def _series(heights, swells=None):
    """A wave series shaped like wave_forecast's, for the derivation."""
    from datetime import datetime, timedelta, timezone

    from tools.weather.wave_forecast import WaveSample

    start = datetime.now(timezone.utc)
    return [
        WaveSample(
            time=start + timedelta(hours=i),
            significant_height_m=h,
            period_s=8.0,
            swell_wave_height_m=(swells[i] if swells else None),
        )
        for i, h in enumerate(heights)
    ]


def test_a_calm_sea_derives_nothing_and_says_so():
    """Below every published band is a real result, not a missing one.

    This is the distinction the whole alerts path is built on. An empty list
    here licenses the negative finding "no high wave alert is in force"; a
    failure to look does not, and the two must never converge.
    """
    from alerts.derived import derive_wave_advisories

    advisories, notes = derive_wave_advisories(_series([0.4, 0.6, 0.5]), "Nagapattinam")
    assert advisories == []
    assert any("below every band" in n for n in notes)


def test_the_published_bands_are_applied_not_guessed():
    """3.0-3.5 m is INCOIS's alert band; above 3.5 m is their warning.

    Read from config/risk_thresholds.yaml, where the numbers are tagged
    `provenance: published` and carry the date they were verified. Hardcoding
    them here would let the config and the behaviour drift apart, and the
    config is the thing we open when a judge asks why 3.5.
    """
    from alerts.derived import derive_wave_advisories

    alert, _ = derive_wave_advisories(_series([1.0, 3.2, 2.0]), "Nagapattinam")
    assert [a["severity"] for a in alert] == ["advisory"]
    assert alert[0]["type"] == "high_wave"

    warning, _ = derive_wave_advisories(_series([1.0, 3.9]), "Nagapattinam")
    assert [a["severity"] for a in warning] == ["warning"]

    # The peak drives it, not the average: a boat meets the worst hour it is
    # out in, not the mean of the day.
    assert derive_wave_advisories(_series([0.2, 0.2, 3.9]), "X")[0][0]["severity"] == "warning"


def test_swell_uses_its_own_lower_band():
    """2.5 m of swell is an advisory; 2.5 m of significant height is not.

    Long-period swell breaks harder inshore than its height suggests, which
    is why INCOIS publishes a separate lower band for it.
    """
    from alerts.derived import derive_wave_advisories

    advisories, _ = derive_wave_advisories(
        _series([2.6, 2.6], swells=[2.6, 2.6]), "Nagapattinam"
    )
    kinds = {a["type"] for a in advisories}
    assert kinds == {"swell_surge"}, "the same height must not trip the Hs band"


def test_a_missing_swell_component_is_not_reported_as_calm():
    """Open-Meteo does not always return the wind-wave/swell split.

    No swell advisory then means the component was absent, not that the swell
    was small, and the notes have to say which -- otherwise the caller reads
    silence as safety.
    """
    from alerts.derived import derive_wave_advisories

    _, notes = derive_wave_advisories(_series([1.0, 1.2]), "Nagapattinam")
    assert any("must not be reported as clear" in n for n in notes)


def test_a_derived_advisory_never_impersonates_incois():
    """It is our arithmetic on their criteria, and it says so on its face."""
    from alerts.derived import DERIVED_AUTHORITY, derive_wave_advisories

    advisories, _ = derive_wave_advisories(_series([4.0]), "Nagapattinam")
    assert advisories
    for advisory in advisories:
        assert advisory["authority"] == DERIVED_AUTHORITY
        assert "ORCA" in advisory["authority"]
        assert "not read" in advisory["text"], (
            "the text must state that the INCOIS bulletin itself was not consulted"
        )


def test_an_operator_bulletin_outranks_our_arithmetic(monkeypatch):
    """A human who read the actual authority wins, at equal severity.

    Especially when the two disagree. Our derivation is corroboration for a
    published bulletin, never a replacement for one.
    """
    from datetime import datetime, timedelta, timezone

    from alerts.derived import DERIVED_AUTHORITY
    from tools.weather import alerts as alerts_module

    now = datetime.now(timezone.utc)
    transcribed = alerts_module.Advisory(
        type="high_wave",
        severity="warning",
        zone="Nagapattinam",
        issued_at=now,
        valid_until=now + timedelta(hours=12),
        authority="INCOIS",
        text="High Wave Alert transcribed from the bulletin.",
    )
    monkeypatch.setattr(gdacs, "load_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        alerts_module, "_load_operator_table", lambda: ([transcribed], now, [])
    )
    monkeypatch.setattr(
        alerts_module,
        "_derive_wave",
        lambda *a, **k: (
            [
                {
                    "type": "high_wave",
                    "severity": "warning",
                    "zone": "Nagapattinam",
                    "issued_at": now,
                    "valid_until": now + timedelta(hours=12),
                    "authority": DERIVED_AUTHORITY,
                    "text": "derived",
                }
            ],
            [],
        ),
    )
    out = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON, district="Nagapattinam"))
    high = [a for a in out.alerts if a.type == "high_wave"]
    assert len(high) == 1, "the same advisory must not be counted twice"
    assert high[0].authority == "INCOIS", "the transcribed bulletin must win"


def test_wave_types_stay_checked_when_the_operator_table_goes_stale(monkeypatch):
    """The whole point. A stale YAML file no longer blinds the wave half.

    Before this, the table stopped counting as a check after twenty-four
    hours, so once nobody had opened it for a day every answer became
    cyclone-only -- silently, and on the exact days a demo is most likely to
    happen.
    """
    from tools.weather import alerts as alerts_module

    monkeypatch.setattr(gdacs, "load_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        alerts_module,
        "_load_operator_table",
        lambda: ([], None, ["operator table last reviewed too long ago"]),
    )
    out = active_alerts(ActiveAlertsIn(lat=LAT, lon=LON))
    if "no cached wave forecast" in " ".join(out.quality.notes):
        pytest.skip("no wave cache present; run scripts/refresh_cache.py --weather")
    assert set(out.checked_types) == {"high_wave", "swell_surge"}
    assert "cyclone" not in out.checked_types, (
        "a wave forecast is not evidence about a cyclone"
    )
    assert out.checked is True
