"""Tests for /readiness honesty and the cache reads behind it.

Every test here is hermetic: cache files are built in tmp dirs or stubbed in
memory, and no test touches the network. Each test fails on the pre-fix code:

* a corrupt Open-Meteo file raised instead of reading as a miss (500ing
  /readiness on one bad file);
* a days-old weather cache still reported ``cached: True`` with no staleness
  concept, so the morning after a missed nightly run the UI read healthy;
* the alerts row reported the GDACS cyclone check while saying nothing about
  the operator wave table, so a stale table passed as a full check.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from core import config
from ingest.sources import erddap, gdacs, open_meteo
from orchestrator.routes import health
from tools.weather import alerts as alerts_module


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _marine(fetched_at: datetime) -> open_meteo.CachedResponse:
    return open_meteo.CachedResponse(
        endpoint="marine",
        url="https://example.invalid/marine",
        params={"latitude": 10.77, "longitude": 79.84},
        fetched_at=fetched_at,
        payload={"latitude": 10.79166, "longitude": 79.95836, "hourly": {}},
    )


def _grid(dataset_id: str, age_days: float) -> erddap.GridSlice:
    now = _now()
    return erddap.GridSlice(
        dataset_id=dataset_id,
        variable="analysed_sst",
        units="degree_C",
        url="https://example.invalid/griddap",
        fetched_at=now,
        time=(now - timedelta(days=age_days)).isoformat(),
        values=[],
        requested_cells=0,
        resolution_deg=0.05,
        gap_filled=True,
        authority="test",
    )


# Captured at import, before any monkeypatch replaces config.load_yaml.
_REAL_LOAD_YAML = config.load_yaml


def _fake_load_yaml(*, last_reviewed: str | None, max_age: float = 24.0, missing: bool = False):
    """Fake config.load_yaml that controls only the operator table.

    Everything else delegates to the real loader, so the real freshness
    thresholds (12 h forecast, 7 d satellite) stay in force and the tests pin
    behaviour against them rather than against copies.
    """
    real = _REAL_LOAD_YAML

    def fake(name: str):
        if name == "active_alerts.yaml":
            if missing:
                raise FileNotFoundError("Missing config file (test)")
            return {
                "meta": {"last_reviewed": last_reviewed, "review_max_age_hours": max_age},
                "advisories": [],
            }
        return real(name)

    return fake


def _stub_readiness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    marine: open_meteo.CachedResponse | None,
    grids: dict[str, erddap.GridSlice | None] | None = None,
    advisories: tuple[list, datetime] | None = None,
    last_reviewed: str | None = None,
    operator_missing: bool = False,
):
    """Point every I/O behind readiness at hermetic fakes."""
    monkeypatch.setattr(open_meteo, "load_cached", lambda *a, **k: marine)
    grids = grids if grids is not None else {}
    monkeypatch.setattr(erddap, "load_cached", lambda dataset_id: grids.get(dataset_id))
    monkeypatch.setattr(gdacs, "load_cached", lambda *a, **k: advisories)
    monkeypatch.setattr(health, "provider_status", dict)
    monkeypatch.setattr(
        config, "load_yaml", _fake_load_yaml(last_reviewed=last_reviewed, missing=operator_missing)
    )


def _reviewed(hours_ago: float) -> str:
    return (_now() - timedelta(hours=hours_ago)).isoformat()


# ==========================================================================
# open_meteo.load_cached never raises on a bad file
# ==========================================================================


def test_corrupt_open_meteo_cache_reads_as_missing(tmp_path, monkeypatch):
    """A half-written file is 'nothing cached', not a 500 from /readiness."""
    monkeypatch.setattr(open_meteo, "CACHE_DIR", tmp_path)
    (tmp_path / "marine_10.77_79.84_20260907.json").write_text("{not json", encoding="utf-8")
    assert open_meteo.load_cached("marine", 10.77, 79.84) is None


def test_wrong_shaped_open_meteo_cache_reads_as_missing(tmp_path, monkeypatch):
    """Valid JSON with the wrong shape (or a bad timestamp) is also a miss."""
    monkeypatch.setattr(open_meteo, "CACHE_DIR", tmp_path)
    path = tmp_path / "marine_10.77_79.84_20260907.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert open_meteo.load_cached("marine", 10.77, 79.84) is None

    path.write_text(
        json.dumps(
            {
                "endpoint": "marine",
                "url": "https://example.invalid",
                "params": {},
                "fetched_at": "not-a-time",
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    assert open_meteo.load_cached("marine", 10.77, 79.84) is None


# ==========================================================================
# weather freshness gate (forecast_max_age_h = 12, from the real config)
# ==========================================================================


def test_fresh_weather_is_ready(monkeypatch):
    _stub_readiness(
        monkeypatch, marine=_marine(_now() - timedelta(minutes=24)), last_reviewed=_reviewed(1)
    )
    out = health.readiness()
    assert out["ready"] is True
    assert out["hint"] is None
    weather = out["layers"]["weather"]
    assert weather["cached"] is True
    assert weather["stale"] is False
    assert weather["age_hours"] == pytest.approx(0.4, abs=0.1)


def test_stale_weather_is_not_cached_and_blocks_ready(monkeypatch):
    """A 13 h old forecast must not read as a healthy cache the next morning."""
    _stub_readiness(
        monkeypatch, marine=_marine(_now() - timedelta(hours=13)), last_reviewed=_reviewed(1)
    )
    out = health.readiness()
    weather = out["layers"]["weather"]
    assert weather["cached"] is False
    assert weather["stale"] is True
    assert weather["age_hours"] == pytest.approx(13.0, abs=0.1)
    assert out["ready"] is False
    assert "stale" in out["hint"]


def test_missing_weather_is_not_ready(monkeypatch):
    _stub_readiness(monkeypatch, marine=None, last_reviewed=_reviewed(1))
    out = health.readiness()
    assert out["layers"]["weather"] == {
        "cached": False,
        "retrieved_at": None,
        "age_hours": None,
        "stale": False,
        "max_age_hours": 12.0,
    }
    assert out["ready"] is False
    assert out["hint"] == "run: python scripts/refresh_cache.py"


# ==========================================================================
# ocean layers: missing stays missing, stale is flagged, never clear
# ==========================================================================


def test_ocean_layers_report_missing_when_there_is_no_cache(monkeypatch):
    _stub_readiness(monkeypatch, marine=_marine(_now()), last_reviewed=_reviewed(1))
    out = health.readiness()
    for label in ("sst", "chlorophyll"):
        assert out["layers"][label]["cached"] is False
        assert out["layers"][label]["observation_age_days"] is None
        assert out["layers"][label]["stale"] is False


def test_stale_ocean_observation_is_flagged_not_cached(monkeypatch):
    """A 10 d old SST field is past the 7 d composite limit: not clear."""
    _stub_readiness(
        monkeypatch,
        marine=_marine(_now()),
        grids={"jplMURSST41": _grid("jplMURSST41", 10.0)},
        last_reviewed=_reviewed(1),
    )
    out = health.readiness()
    sst = out["layers"]["sst"]
    assert sst["observation_age_days"] == pytest.approx(10.0, abs=0.1)
    assert sst["stale"] is True
    assert sst["cached"] is False
    # The other layer is still honestly missing.
    assert out["layers"]["chlorophyll"]["cached"] is False


def test_fresh_ocean_observation_is_cached(monkeypatch):
    _stub_readiness(
        monkeypatch,
        marine=_marine(_now()),
        grids={"jplMURSST41": _grid("jplMURSST41", 1.0)},
        last_reviewed=_reviewed(1),
    )
    out = health.readiness()
    assert out["layers"]["sst"]["cached"] is True
    assert out["layers"]["sst"]["stale"] is False


# ==========================================================================
# alerts row: cyclone check plus operator-table freshness, both shown
# ==========================================================================


def test_alerts_row_surfaces_a_stale_operator_table(monkeypatch):
    """GDACS fresh but the wave table 43 h overdue: both facts, no masking."""
    now = _now()
    _stub_readiness(
        monkeypatch,
        marine=_marine(now),
        advisories=([], now),
        last_reviewed=_reviewed(43),
    )
    out = health.readiness()
    alerts = out["layers"]["alerts"]
    assert alerts["cached"] is True  # the cyclone negative finding stands
    assert alerts["in_force"] == 0
    assert alerts["age_hours"] == pytest.approx(0.0, abs=0.1)
    assert alerts["operator_checked"] is False
    assert alerts["operator_age_hours"] == pytest.approx(43.0, abs=0.5)


def test_alerts_row_reports_a_fresh_operator_table(monkeypatch):
    now = _now()
    _stub_readiness(
        monkeypatch,
        marine=_marine(now),
        advisories=([], now),
        last_reviewed=_reviewed(1),
    )
    alerts = health.readiness()["layers"]["alerts"]
    assert alerts["cached"] is True
    assert alerts["operator_checked"] is True


def test_missing_operator_table_is_unchecked_not_clear(monkeypatch):
    now = _now()
    _stub_readiness(
        monkeypatch, marine=_marine(now), advisories=([], now), operator_missing=True
    )
    alerts = health.readiness()["layers"]["alerts"]
    assert alerts["cached"] is True
    assert alerts["operator_checked"] is False
    assert alerts["operator_age_hours"] is None


def test_operator_status_agrees_with_active_alerts_rule(monkeypatch):
    """Readiness must not drift from the rule active_alerts enforces.

    The authority is tools/weather/alerts.py::_load_operator_table: over-limit
    means unchecked. If that rule ever changes, this test forces the readiness
    mirror to follow it.
    """
    for hours_ago, expected in ((1.0, True), (43.0, False)):
        monkeypatch.setattr(
            config, "load_yaml", _fake_load_yaml(last_reviewed=_reviewed(hours_ago))
        )
        status = health._operator_table_status(_now())  # private mirror under test
        _, reviewed, _ = alerts_module._load_operator_table()
        assert status["checked"] is expected
        assert (reviewed is not None) is expected
