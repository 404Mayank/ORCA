"""POST /settings changes the tier the next question actually uses.

The tier switch in the UI must move something real, so these tests pin the
plumbing: the override wins over the environment, clearing it returns
authority to ORCA_TIER, and an unknown tier is a 422 that changes nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.narrate import set_template_fallback
from orchestrator import collaborate
from orchestrator import session as session_state
from orchestrator.executor import set_default_timeout_s
from orchestrator.llm import client as llm
from orchestrator.main import app


@pytest.fixture()
def clean_tier(monkeypatch):
    """Scrub both tier sources, restore the override afterwards."""
    monkeypatch.delenv("ORCA_TIER", raising=False)
    llm.set_tier(None)
    llm.set_llm_timeout_s(None)
    collaborate.set_deliberating(None)
    collaborate.set_max_rounds(None)
    collaborate.set_max_added_steps(None)
    session_state.set_context_ttl_minutes(None)
    session_state.set_pending_ttl_minutes(None)
    set_template_fallback(None)
    set_default_timeout_s(None)
    yield
    llm.set_tier(None)
    llm.set_llm_timeout_s(None)
    collaborate.set_deliberating(None)
    collaborate.set_max_rounds(None)
    collaborate.set_max_added_steps(None)
    session_state.set_context_ttl_minutes(None)
    session_state.set_pending_ttl_minutes(None)
    set_template_fallback(None)
    set_default_timeout_s(None)
    monkeypatch.delenv("ORCA_TIER", raising=False)


@pytest.fixture()
def api():
    return TestClient(app)


def test_default_tier_is_free_from_default(api, clean_tier):
    body = api.get("/settings").json()
    assert body["tier"] == "free"
    assert body["tier_source"] == "default"
    assert body["tiers"] == ["free", "fast", "paid"]
    assert isinstance(body["provider_order"], list) and body["provider_order"]
    assert isinstance(body["providers"], dict)


def test_env_tier_reports_env_source(api, clean_tier, monkeypatch):
    monkeypatch.setenv("ORCA_TIER", "fast")
    body = api.get("/settings").json()
    assert body["tier"] == "fast"
    assert body["tier_source"] == "env"


def test_reset_returns_knobs_to_defaults(api, clean_tier):
    api.post("/settings", json={"max_rounds": 0, "context_ttl_min": 30})
    assert api.get("/settings").json()["max_rounds"] == 0
    body = api.post("/settings", json={"reset": ["max_rounds", "context_ttl_min"]}).json()
    assert body["max_rounds"] == 2
    assert body["context_ttl_min"] == 90


def test_reset_unknown_name_is_422(api, clean_tier):
    response = api.post("/settings", json={"reset": ["turbo"]})
    assert response.status_code == 422


def test_env_tier_is_honoured(api, clean_tier, monkeypatch):
    monkeypatch.setenv("ORCA_TIER", "paid")
    body = api.get("/settings").json()
    assert body["tier"] == "paid"
    assert body["tier_source"] == "env"


def test_post_override_wins_over_env(api, clean_tier, monkeypatch):
    monkeypatch.setenv("ORCA_TIER", "free")
    body = api.post("/settings", json={"tier": "paid"}).json()
    assert body["tier"] == "paid"
    assert body["tier_source"] == "app"
    assert llm.tier() == "paid"
    # ... and the next question would use it: same function complete() reads.
    assert "opencode-go" in llm.effective_order()


def test_post_null_returns_authority_to_env(api, clean_tier, monkeypatch):
    monkeypatch.setenv("ORCA_TIER", "fast")
    api.post("/settings", json={"tier": "paid"})
    assert api.get("/settings").json()["tier_source"] == "app"
    body = api.post("/settings", json={"tier": None}).json()
    assert body["tier"] == "fast"
    assert body["tier_source"] == "env"


def test_unknown_tier_is_422_and_changes_nothing(api, clean_tier):
    response = api.post("/settings", json={"tier": "turbo"})
    assert response.status_code == 422
    assert api.get("/settings").json()["tier"] == "free"


def test_deliberation_defaults_on(api, clean_tier):
    assert api.get("/settings").json()["deliberating"] is True
    assert collaborate.deliberating_enabled() is True


def test_deliberation_toggle_round_trips(api, clean_tier):
    assert api.post("/settings", json={"deliberating": False}).json()["deliberating"] is False
    assert collaborate.deliberating_enabled() is False
    assert api.post("/settings", json={"deliberating": True}).json()["deliberating"] is True


def test_deliberation_omitted_leaves_value(api, clean_tier):
    api.post("/settings", json={"deliberating": False})
    body = api.post("/settings", json={"tier": "fast"}).json()
    assert body["deliberating"] is False
    assert body["tier"] == "fast"


def test_knob_defaults(api, clean_tier):
    body = api.get("/settings").json()
    assert body["context_ttl_min"] == 90
    assert body["pending_ttl_min"] == 15
    assert body["max_rounds"] == 2
    assert body["max_added_steps"] == 4
    assert body["template_fallback"] is True
    assert body["llm_timeout_s"] == 30.0
    assert body["step_timeout_s"] == 20.0


def test_knobs_round_trip(api, clean_tier):
    patch = {
        "context_ttl_min": 45,
        "pending_ttl_min": 5,
        "max_rounds": 0,
        "max_added_steps": 1,
        "template_fallback": False,
        "llm_timeout_s": 10,
        "step_timeout_s": 60,
    }
    body = api.post("/settings", json=patch).json()
    for key, value in patch.items():
        assert body[key] == value, key
    # And the live getters agree -- the next question would use these.
    assert session_state.context_ttl_minutes() == 45
    assert session_state.pending_ttl_minutes() == 5
    assert collaborate.max_rounds() == 0
    assert collaborate.max_added_steps() == 1
    assert llm.llm_timeout_s() == 10


def test_knobs_omitted_leave_values(api, clean_tier):
    api.post("/settings", json={"max_rounds": 1, "llm_timeout_s": 12})
    body = api.post("/settings", json={"tier": "fast"}).json()
    assert body["max_rounds"] == 1
    assert body["llm_timeout_s"] == 12
    assert body["max_added_steps"] == 4


def test_knob_out_of_range_is_422_and_changes_nothing(api, clean_tier):
    for bad in [
        {"context_ttl_min": 1},
        {"context_ttl_min": 999},
        {"pending_ttl_min": 0},
        {"max_rounds": -1},
        {"max_rounds": 5},
        {"max_added_steps": 9},
        {"llm_timeout_s": 1},
        {"step_timeout_s": 500},
    ]:
        assert api.post("/settings", json=bad).status_code == 422, bad
    body = api.get("/settings").json()
    assert body["max_rounds"] == 2
    assert body["llm_timeout_s"] == 30.0
