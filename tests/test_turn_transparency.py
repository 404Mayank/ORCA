"""Transparency fixes: per-turn logging, honest fallback badge, suggestion hygiene.

Three small findings from the static-answers diagnosis, each pinned here:

1. Fallback frequency was unmeasurable -- no log line records how a turn was
   planned. Now one logger.info per turn carries provider, attempts, the
   fallback flag and the narration source (counts only, no user text).
2. The clarification-answer fast path wore the "fallback plan" badge although
   nothing failed. A deterministic plan from answered slots must not badge.
3. The model repeats chat suggestions within one reply. Deduped in _chat;
   previous-turn de-repeat is out of reach (session Turns store no options).
"""

from __future__ import annotations

import json
import logging

from agents import intent_planner_agent as planner
from agents.intent_planner_agent import _chat, _use_fallback, plan_query
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from orchestrator.llm.client import LLMResult


def _stub_all_fail(monkeypatch):
    def fake_complete(role, system, user):
        return LLMResult(ok=False, provider="none", model="", error="no key (test)")

    monkeypatch.setattr(planner.llm, "complete", fake_complete)


def test_turn_logs_how_it_was_planned(monkeypatch, caplog):
    """One single-line log per turn: state, provider, attempts, fallback flag."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    with caplog.at_level(logging.INFO, logger="orchestrator.turn"):
        result = run_turn("is it safe to go out?", session_id="tlog-1")
    assert result.state == "clarification"
    records = [r for r in caplog.records if r.name == "orchestrator.turn"]
    assert records, "run_turn must log one line per turn"
    line = records[0].getMessage()
    assert f"turn={result.turn_id}" in line
    assert "state=clarification" in line
    assert "fallback=False" in line
    assert "\n" not in line, "the turn log must stay single-line"


def test_fallback_turn_logs_the_flag(monkeypatch, caplog):
    """A genuine fallback (no model, full slots via keywords) logs fallback=True."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    with caplog.at_level(logging.INFO, logger="orchestrator.turn"):
        result = run_turn(
            "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?",
            session_id="tlog-2",
        )
    assert result.used_fallback_plan is True
    records = [r for r in caplog.records if r.name == "orchestrator.turn"]
    assert records and "fallback=True" in records[-1].getMessage()


def _full_intent() -> Intent:
    return Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="kattumaram",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.KATTUMARAM,
    )


def test_answered_clarification_is_not_a_fallback():
    """The deterministic fast path plans without a model -- and says so."""
    result = _use_fallback(_full_intent(), ["slots complete"], 0, "session", "", deterministic=True)
    assert result.state == "plan"
    assert result.used_fallback is False, "a healthy deterministic path must not wear the badge"
    assert any("deterministic" in n for n in result.notes)


def test_genuine_fallback_still_badges():
    """The flag survives where something actually failed."""
    result = _use_fallback(_full_intent(), ["no LLM"], 0, "keyword", "")
    assert result.used_fallback is True


def test_chat_suggestions_deduped_within_turn():
    out = _chat("hello", "Hello!", ["What is safe?", "What is safe?", "Where are fish?", "Where are fish?", "Why catch?"])
    assert out.chat is not None
    assert out.chat.suggestions == ["What is safe?", "Where are fish?", "Why catch?"]


def test_chat_suggestions_still_cap_at_four():
    out = _chat("hello", "Hello!", ["a", "b", "c", "d", "e"])
    assert out.chat is not None
    assert out.chat.suggestions == ["a", "b", "c", "d", "e"][:4]


def test_model_suggestions_pass_through_dedupe(monkeypatch):
    """End to end on the router path: repeated model suggestions reach the UI once."""
    text = json.dumps({"kind": "chat", "text": "Hi there.", "suggestions": ["Same?", "Same?"]})

    def fake_complete(role, system, user):
        return LLMResult(ok=True, provider="stub", model="m", text=text)

    monkeypatch.setattr(planner.llm, "complete", fake_complete)
    result = plan_query("hello")
    assert result.state == "chat"
    assert result.output.chat is not None
    assert result.output.chat.suggestions == ["Same?"]
