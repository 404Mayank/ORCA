"""Shared fixtures.

The one that matters here is :func:`no_alert_sources`.

Several tests protect the most important invariant in the codebase: **"we could
not check" must never be reported as "there is nothing there".** They used to
verify it by calling ``active_alerts()`` and relying on it failing, because no
alert source existed. That made the world's brokenness load-bearing, and when
GDACS was wired on 2026-09-06 those tests began asserting the opposite of the
truth -- the same rot that hit two planner tests when the ocean tools landed.

A test for "what happens when nothing can be checked" must *create* that
condition, not wait for it. This fixture does.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    """Scrub credentials so the suite never touches live services.

    core.env auto-loads .env at import, which is what the app wants -- but a
    test reaching llm.complete (or Supabase) without an explicit stub would
    then spend real money and minutes: found 2026-09-07 when collaboration
    tests started deliberating against live models after a .env appeared.
    Tests that need a key set it themselves via monkeypatch; everything else
    runs hermetic.
    """
    for var in list(os.environ):
        if var.startswith(("OPENCODE_", "GROQ_", "ANTHROPIC_")) or var in {
            "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY",
            "DATABASE_URL", "COPERNICUS_USERNAME", "COPERNICUS_PASSWORD",
        }:
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def no_alert_sources(monkeypatch):
    """Make every alert source unreachable for the duration of a test.

    Simulates the genuine outage case: GDACS unreachable or its cache stale,
    and an operator table nobody has reviewed inside the window. Both are
    realistic -- the venue loses internet, and a table is only as fresh as the
    last person to open it.
    """
    from ingest.sources import gdacs
    from tools.weather import alerts as alerts_module

    monkeypatch.setattr(gdacs, "load_cached", lambda *a, **k: None)
    monkeypatch.setattr(
        alerts_module,
        "_load_operator_table",
        lambda: ([], None, ["operator table unavailable (test fixture)"]),
    )
    return True


@pytest.fixture
def one_cyclone_in_force(monkeypatch):
    """A severe cyclone advisory in force, as GDACS would report it.

    Lets the "an advisory outranks our arithmetic" path be tested without
    waiting for an actual cyclone, which is not a reasonable test dependency.
    """
    from datetime import datetime, timedelta, timezone

    from ingest.sources import gdacs

    now = datetime.now(timezone.utc)
    advisory = {
        "type": "cyclone",
        "severity": "severe",
        "zone": "India, Sri Lanka",
        "issued_at": now - timedelta(hours=6),
        "valid_until": now + timedelta(hours=36),
        "authority": "GDACS",
        "text": "TESTSTORM-26: Tropical Cyclone (maximum wind speed of 150 km/h) (GDACS Red)",
        "_url": "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?test",
    }
    monkeypatch.setattr(gdacs, "load_cached", lambda *a, **k: ([advisory], now))
    return advisory
