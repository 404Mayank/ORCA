"""Tamil Slice 1 plumbing: an unsupported answer language falls back loudly.

Slice 1 is UI chrome only -- Tamil menus, Tamil font, a persisted switch.
Answer prose stays English until the templates exist. The contract pinned
here: ``run_turn(..., language="ta")`` answers in English, records *why* in
``notes``, and never raises. Without the pin at the narrate call site,
``language.render`` raises ``NotImplementedError`` out of the turn, which is
exactly what the no-raise test below catches.
"""

from __future__ import annotations

from agents import intent_planner_agent as planner
from orchestrator.llm.client import LLMResult

_SAFETY_Q = "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"


def _stub_all_fail(monkeypatch):
    def fake_complete(role, system, user):
        return LLMResult(ok=False, provider="none", model="", error="no key (test)")

    monkeypatch.setattr(planner.llm, "complete", fake_complete)


def test_ta_answers_in_english_with_recorded_note(monkeypatch):
    """language='ta' answers (English template tier) and says why."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-ta-1", language="ta")
    assert result.state == "answer"
    assert result.answer and result.answer.isascii(), (
        "Slice 1 answers stay English; Tamil prose needs templates that do not exist yet"
    )
    assert any("ta" in note and "English" in note for note in result.notes), (
        f"the fallback must be recorded, never silent; notes={result.notes!r}"
    )


def test_unknown_language_never_raises(monkeypatch):
    """Any unsupported tag degrades to English, not to a traceback."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-xx-1", language="xx")
    assert result.state == "answer"
    assert result.answer


def test_en_has_no_fallback_note(monkeypatch):
    """The default path is untouched: no note, no behaviour change."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-en-1", language="en")
    assert result.state == "answer"
    assert not [n for n in result.notes if "not supported yet" in n]
