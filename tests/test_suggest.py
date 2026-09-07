"""Contextual follow-up suggestions for answer turns.

Planner-written suggestions are pre-answer guesses; this module generates
them post-verify from the finished recommendation. Every test here fails
without its code (verified by stash A/B): the suite is hermetic, so the
model path is a stub and the deterministic floor must hold on its own.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from agents.suggest import (
    Suggestions,
    _assemble,
    apply_hygiene,
    clean_daypart,
    humanize_vessel,
    parse_model_suggestions,
    rule_suggestions,
    suggest_followups,
)
from core.schemas.intent import Intent, QueryType, SpatialReference, TimeWindow, VesselClass
from orchestrator.llm import client as llm_client
from orchestrator.llm.client import LLMResult


def _intent(**over) -> Intent:
    base = {
        "query_type": QueryType.SAFETY_ASSESS,
        "raw_query": "is it safe?",
        "spatial_reference": SpatialReference(name="Nagapattinam"),
        "vessel_class": VesselClass.FRP_9M,
        "time_window": TimeWindow(
            start=datetime(2026, 9, 8, 5, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            end=datetime(2026, 9, 8, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            phrase="tomorrow morning",
        ),
    }
    base.update(over)
    return Intent(**base)


def _rec():
    from fixtures.safety_assess_nagapattinam import build_ideal_safety_answer

    return build_ideal_safety_answer()


def _stub_complete(monkeypatch, text=None, *, ok=True):
    # provider_status gates the model attempt: with the hermetic scrub in
    # force every provider reads absent, so model-path tests set a key.
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")

    def fake_complete(role, system, user):
        assert role == "suggest", "suggestions must use the dedicated suggest role"
        return LLMResult(ok=ok, text=text or "", provider="stub", model="stub", error=None if ok else "down")

    monkeypatch.setattr(llm_client, "complete", fake_complete)


def _stub_no_provider(monkeypatch):
    monkeypatch.setattr(llm_client, "provider_status", lambda: {k: False for k in ("opencode", "groq")})


# ==========================================================================
# Shape and cap
# ==========================================================================


def test_model_suggestions_capped_at_four(monkeypatch):
    _stub_complete(monkeypatch, '["one question here please", "two question here please", "three question here please", "four question here please", "five question here please", "six question here please"]')
    out = suggest_followups(_rec(), _intent())
    assert len(out.texts) <= 4


def test_parse_accepts_list_dict_and_fences():
    assert parse_model_suggestions('["a question here please"]') == ["a question here please"]
    assert parse_model_suggestions('{"suggestions": ["a question here please"]}') == ["a question here please"]
    assert parse_model_suggestions('```json\n["a question here please"]\n```') == ["a question here please"]
    assert parse_model_suggestions("not json at all") == []
    assert parse_model_suggestions("") == []


# ==========================================================================
# Number stripping + readability (the driver-display lesson, repeated)
# ==========================================================================


def test_digits_never_survive_hygiene():
    dirty = ["Waves over 3 m tomorrow?", "Go out 5 km past the line", "Safe at 05:00?"]
    for text in apply_hygiene(dirty):
        assert not re.search(r"\d", text), f"digit leaked: {text!r}"


def test_hygiene_drops_fragments_not_questions():
    assert apply_hygiene(["ok", "  ", " waves "]) == []
    good = apply_hygiene(["When should my boat be back ashore?"])
    assert good == ["When should my boat be back ashore?"]


def test_hygiene_rejects_mangled_remnants():
    # "next 2 days" stripped in place would read "next  days" -- that is a
    # broken button, not a cleaned one.
    assert apply_hygiene(["fish the next 2 days please"]) == []


def test_model_digits_stripped_end_to_end(monkeypatch):
    _stub_complete(monkeypatch, '["Is 15 kn too much for my boat today?"]')
    out = suggest_followups(_rec(), _intent())
    for text in out.texts:
        assert not re.search(r"\d", text)


# ==========================================================================
# Humanize map + phrase rule
# ==========================================================================


def test_humanize_vessel_has_no_digits():
    for value in ("kattumaram", "frp_9m", "mechanised_trawler"):
        label = humanize_vessel(value)
        assert label and not re.search(r"\d", label), label
    assert humanize_vessel("frp_9m") == "FRP boat"


def test_clean_daypart_passes_clean_and_degrades_dirty():
    assert clean_daypart("tomorrow morning") == "tomorrow morning"
    assert clean_daypart("next 2 days") == "the coming days"
    assert clean_daypart(None) == "the coming days"
    assert "  " not in clean_daypart("next 2 days")


def test_rule_suggestions_reference_context_without_digits():
    texts = rule_suggestions(_intent())
    assert texts, "a fully-specified intent must yield rule suggestions"
    assert len(texts) <= 8
    for text in texts:
        assert not re.search(r"\d", text), text
    assert any("Nagapattinam" in t for t in texts)
    assert any("FRP boat" in t for t in texts)


def test_rule_templates_skip_missing_slots():
    texts = rule_suggestions(_intent(spatial_reference=None, vessel_class=None))
    assert texts, "generic templates must still fire without slots"


# ==========================================================================
# Dedupe, backfill, de-repeat
# ==========================================================================


def test_dedupe_and_cap_in_hygiene():
    q = "When should my boat be back ashore?"
    out = apply_hygiene([q, q, q])
    assert out == [q]


def test_backfill_reaches_four_from_rules(monkeypatch):
    _stub_complete(monkeypatch, "[]", ok=True)
    out = suggest_followups(_rec(), _intent())
    assert out.source in ("rules", "mixed")
    assert 1 <= len(out.texts) <= 4


def test_backfill_refiltered_against_prior_and_itself(monkeypatch):
    _stub_no_provider(monkeypatch)
    from agents.intent_planner_agent import SUGGESTIONS

    out = suggest_followups(_rec(), _intent(), prior_options=list(SUGGESTIONS))
    assert not set(out.texts) & set(SUGGESTIONS), "static backfill must not repeat prior buttons"
    assert len(out.texts) == len(set(out.texts))


def test_prior_options_derepeat_model_output(monkeypatch):
    _stub_complete(monkeypatch, '["When should my boat be back ashore?"]')
    out = suggest_followups(
        _rec(), _intent(), prior_options=["When should my boat be back ashore?"]
    )
    assert "When should my boat be back ashore?" not in out.texts


# ==========================================================================
# Source labels
# ==========================================================================


def test_model_only_reports_model(monkeypatch):
    _stub_complete(
        monkeypatch,
        '["When should my boat be back ashore?", "What should we watch for offshore?", "Is the zone still good later?", "Where is the boundary line from here?"]',
    )
    out = suggest_followups(_rec(), _intent())
    # Four surviving model texts: no backfill fired, single source.
    assert out.source == "model"
    assert len(out.texts) == 4


def test_model_shortfall_backfilled_reports_mixed(monkeypatch):
    _stub_complete(monkeypatch, '["When should my boat be back ashore?"]')
    out = suggest_followups(_rec(), _intent())
    assert out.source == "mixed"
    assert len(out.texts) > 1


def test_rules_plus_static_blend_reports_mixed():
    got = _assemble([(["Only one rule here please"], "rules"), (["Only one static here please"], "static")])
    assert got.source == "mixed"
    assert got.texts == ["Only one rule here please", "Only one static here please"]


def test_no_provider_reports_static(monkeypatch):
    _stub_no_provider(monkeypatch)
    out = suggest_followups(_rec(), _intent())
    assert out.source == "static"
    assert out.texts


def test_source_vocabulary_is_closed(monkeypatch):
    _stub_complete(monkeypatch, '["When should my boat be back ashore?"]')
    assert suggest_followups(_rec(), _intent()).source in ("model", "rules", "static", "mixed")


# ==========================================================================
# _chat strip fix (fix-or-state: the fix ships here, the test proves it)
# ==========================================================================


def test_chat_suggestions_stripped():
    from agents.intent_planner_agent import _chat

    out = _chat("hello", "Hi there", ["Is 15 kn too much?", "Is 15 kn too much?"])
    assert out.chat is not None
    for s in out.chat.suggestions:
        assert not re.search(r"\d", s), s
    assert len(out.chat.suggestions) == len(set(out.chat.suggestions))


# ==========================================================================
# Guard order: a failed verifier means no suggest call, ever
# ==========================================================================


def test_verifier_failure_means_no_suggest_call(monkeypatch):
    import orchestrator.turn as turn_module
    from orchestrator.turn import run_turn

    calls: list[str] = []

    from types import SimpleNamespace as _NS

    planning = _NS(
        state="plan",
        output=_NS(intent=_intent()),
        plan=object(),
        intent=_intent(),
        notes=[],
        attempts=0,
        llm_provider="stub",
        llm_model="stub",
        used_fallback=False,
    )
    monkeypatch.setattr(turn_module, "plan_query", lambda *a, **k: planning)
    team = _NS(
        result=_NS(tool_call_log=[]),
        notes=[],
        dropped=[],
        concerns=[],
        rounds=0,
        describe=list,
    )
    monkeypatch.setattr(turn_module, "run_with_collaboration", lambda *a, **k: team)

    class _Rec:
        verdict = None
        caveats: list[str] = []  # noqa: RUF012 -- test stub, never mutated

        def model_copy(self, update=None):
            return self

    monkeypatch.setattr(turn_module, "build_recommendation", lambda *a, **k: _Rec())
    report = _NS(ok=False, errors=["hallucinated"], numbers_checked=0)
    monkeypatch.setattr(turn_module, "verify", lambda *a, **k: report)

    def fake_suggest(*a, **k):
        calls.append("suggest")
        return Suggestions(texts=[], source="rules")

    monkeypatch.setattr(turn_module, "suggest_followups", fake_suggest)
    monkeypatch.setattr(turn_module, "narrate", lambda *a, **k: pytest.fail("narrate must not run"))

    result = run_turn("is it safe?", session_id="sug-guard-1")
    assert result.state == "error"
    assert calls == [], "suggest must never run on an unverified object"
    assert result.options == [] and result.suggestion_source == ""


# ==========================================================================
# Abandon path: a hung suggest call cannot hold the answer
# ==========================================================================


def test_abandoned_suggest_serves_rules_fast(monkeypatch):
    import time
    from types import SimpleNamespace as _NS

    import orchestrator.turn as turn_module
    from agents.narrate import Narration
    from orchestrator.turn import run_turn

    planning = _NS(
        state="plan",
        output=_NS(intent=_intent()),
        plan=object(),
        intent=_intent(),
        notes=[],
        attempts=0,
        llm_provider="stub",
        llm_model="stub",
        used_fallback=False,
    )
    monkeypatch.setattr(turn_module, "plan_query", lambda *a, **k: planning)
    team = _NS(
        result=_NS(tool_call_log=[], degraded=False),
        notes=[],
        dropped=[],
        concerns=[],
        rounds=0,
        deliberations=[],
        describe=list,
    )
    monkeypatch.setattr(turn_module, "run_with_collaboration", lambda *a, **k: team)
    monkeypatch.setattr(turn_module, "build_recommendation", lambda *a, **k: _rec())
    report = _NS(ok=True, errors=[], numbers_checked=3)
    monkeypatch.setattr(turn_module, "verify", lambda *a, **k: report)
    monkeypatch.setattr(
        turn_module, "narrate", lambda *a, **k: Narration(text="safe.", source="template")
    )

    def hung_suggest(*a, **k):
        # Long enough to prove the budget fires; the pool is shut down
        # without waiting, so the suite does not sit through it.
        time.sleep(8)
        return Suggestions(texts=["late question here please"], source="model")

    monkeypatch.setattr(turn_module, "suggest_followups", hung_suggest)
    monkeypatch.setattr(turn_module, "SUGGEST_BUDGET_S", 0.3)

    started = time.monotonic()
    result = run_turn("is it safe?", session_id="sug-abandon-1")
    elapsed = time.monotonic() - started
    assert result.state == "answer"
    assert result.options, "the answer must still carry follow-ups"
    assert result.suggestion_source in ("rules", "static", "mixed")
    assert elapsed < 5, f"abandoned suggest held the answer {elapsed:.1f}s"
