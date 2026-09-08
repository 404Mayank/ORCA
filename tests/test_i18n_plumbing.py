"""Answer-language plumbing through ``run_turn``.

Slice 1 pinned that ``language="ta"`` answered in English and *said so*.
Slice 2 moves the line: Tamil has templates, so a Tamil request is answered
in Tamil with no apology, and the loud-fallback contract now belongs to tags
that genuinely have no renderer.

What must stay true across every slice: an unsupported tag never raises out
of the turn, and a fallback is never silent. A Tamil-flagged answer that
quietly arrives in English is worse than an error, because nothing in the
response says the language was ignored.
"""

from __future__ import annotations

import re

from agents import intent_planner_agent as planner
from orchestrator.llm.client import LLMResult

_SAFETY_Q = "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"

_TAMIL = re.compile(r"[஀-௿]")


def _stub_all_fail(monkeypatch):
    def fake_complete(role, system, user):
        return LLMResult(ok=False, provider="none", model="", error="no key (test)")

    monkeypatch.setattr(planner.llm, "complete", fake_complete)


def test_ta_answers_in_tamil_without_a_fallback_note(monkeypatch):
    """Slice 2: the templates exist, so the answer is Tamil and says nothing
    about falling back -- there was no fallback to declare."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-ta-1", language="ta")
    assert result.state == "answer"
    assert result.answer and _TAMIL.search(result.answer), (
        f"a ta answer must be in Tamil script; got {result.answer!r}"
    )
    assert not [n for n in result.notes if "not supported yet" in n], (
        f"Tamil is supported now; notes={result.notes!r}"
    )


def test_unknown_language_falls_back_to_english_and_records_why(monkeypatch):
    """Any tag without a templates/ directory degrades to English, loudly."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-xx-1", language="xx")
    assert result.state == "answer"
    assert result.answer and result.answer.isascii(), (
        "an unrenderable language answers in English, not in a guess"
    )
    assert any("xx" in note and "English" in note for note in result.notes), (
        f"the fallback must be recorded, never silent; notes={result.notes!r}"
    )


def test_unknown_language_never_raises(monkeypatch):
    """``language.render`` raises on an unknown tag by design; the turn must
    catch that decision rather than propagate a traceback to the caller."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-zz-1", language="zz-Latn-999")
    assert result.state == "answer"
    assert result.answer


def test_en_has_no_fallback_note(monkeypatch):
    """The default path is untouched: no note, no behaviour change."""
    _stub_all_fail(monkeypatch)
    from orchestrator.turn import run_turn

    result = run_turn(_SAFETY_Q, session_id="i18n-en-1", language="en")
    assert result.state == "answer"
    assert not [n for n in result.notes if "not supported yet" in n]
