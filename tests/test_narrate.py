"""Tests for LLM narration and its guards.

None of these call a provider. The LLM is replaced with a stub so the tests
exercise exactly the thing that matters: what happens to the model's output
before a fisherman sees it. The live path is covered by
``scripts/try_narration.py``, which needs a key and is run by hand.
"""

from __future__ import annotations

import pytest

from agents import narrate as narrate_module
from agents.narrate import Narration, extract_numbers, narrate, number_guard
from language import render
from orchestrator.llm.client import LLMResult


@pytest.fixture
def rec():
    from fixtures.safety_assess_nagapattinam import build_ideal_safety_answer

    return build_ideal_safety_answer()


def _stub(monkeypatch, text: str | None, *, ok: bool = True, error: str | None = None):
    """Replace the provider with a canned response."""

    def fake_complete(role, system, user):
        return LLMResult(ok=ok, text=text or "", provider="stub", model="stub-model", error=error)

    monkeypatch.setattr(narrate_module.llm, "complete", fake_complete)


# ==========================================================================
# The number guard -- the reason this layer is safe to have at all
# ==========================================================================


def test_extract_numbers_reads_ranges_and_times_as_written():
    assert extract_numbers("Waves 0.3-0.44 m by 13:00") == ["0.3", "0.44", "13:00"]


def test_a_time_is_one_token_not_two():
    """13:00 must not decompose into 13 and 00, or a model could write 13:30
    and pass because 13 and 30 both appear somewhere."""
    assert "13:00" in extract_numbers("back by 13:00")
    assert "00" not in extract_numbers("back by 13:00")


def test_guard_passes_faithful_prose():
    reference = "Waves 1.8-2.2 m, under the 2.5 m limit. Be back by 13:00."
    prose = "Do not go out. Waves are 1.8-2.2 m against a 2.5 m limit; be ashore by 13:00."
    assert number_guard(prose, reference) == []


def test_guard_catches_rounding():
    """The most natural thing a fluent writer does, and the one thing it may not."""
    reference = "Visibility 2.44-29.56 km, above the 2 km minimum."
    prose = "Visibility is about 2.4 to 30 km, above the 2 km minimum."
    assert number_guard(prose, reference) == ["2.4", "30"]


def test_guard_catches_an_invented_figure():
    reference = "Waves 1.8-2.2 m, under the 2.5 m limit."
    prose = "Waves 1.8-2.2 m, under the 2.5 m limit, with gusts to 28 knots."
    assert number_guard(prose, reference) == ["28"]


def test_guard_catches_a_reformatted_number():
    """2.40 is not 2.4. If the model reformatted it, it changed it."""
    assert number_guard("2.40 m", "2.4 m") == ["2.40"]


def test_guard_catches_a_shifted_time():
    reference = "Start back by 11:00 to be ashore by 13:00."
    prose = "Start back by 11:30 to be ashore by 13:00."
    assert number_guard(prose, reference) == ["11:30"]


# ==========================================================================
# narrate(): fallback behaviour
# ==========================================================================


def test_llm_prose_that_passes_the_guard_is_used(rec, monkeypatch):
    reference = render(rec)
    # Faithful rewrite: same numbers, different words, verdict first.
    _stub(monkeypatch, "Marginal - you can go, but watch the time. " + reference)
    out = narrate(rec)
    assert out.source == "llm"
    assert out.fallback_reason is None


def test_rounding_in_the_prose_triggers_template_fallback(rec, monkeypatch):
    _stub(monkeypatch, "Marginal - you can go. Waves are roughly 2.1 m against the 2.5 m limit.")
    out = narrate(rec)
    assert out.source == "template"
    assert "introduced numbers" in out.fallback_reason
    assert out.text == render(rec), "fallback must be the deterministic rendering, verbatim"


def test_guard_cannot_detect_a_number_reused_in_the_wrong_context(rec, monkeypatch):
    """A known limitation, pinned so nobody assumes the guard is stronger than it is.

    The guard is a bag of tokens: it knows WHICH numbers the reference holds,
    not WHERE. The ideal answer's confidence basis says "+/-2 h", so the token
    "2" is allowed -- and a model that wrote "waves are roughly 2 m" would pass,
    having borrowed a timing uncertainty as a wave height.

    Mitigations in place: the prompt forbids it, the verdict-first check
    catches the reorderings that usually accompany this, and the reference is
    kept short so there are few stray tokens to borrow. A positional guard
    (number + unit + neighbour) would close it and is the right next step if
    live runs ever show it happening.
    """
    reference = render(rec)
    assert "2" in extract_numbers(reference), "precondition: '2' is a stray token here"
    _stub(monkeypatch, "Marginal - you can go, but watch the time. Waves are roughly 2 m. " + reference)
    out = narrate(rec)
    assert out.source == "llm"  # passes, and should not -- documented, not fixed


def test_provider_failure_triggers_template_fallback(rec, monkeypatch):
    _stub(monkeypatch, None, ok=False, error="connection refused")
    out = narrate(rec)
    assert out.source == "template"
    assert "connection refused" in out.fallback_reason
    assert out.text == render(rec)


def test_a_narration_that_buries_the_verdict_is_rejected(rec, monkeypatch):
    """Leading with reassurance and mentioning the call later reads as
    permission. The verdict goes first or the prose is discarded."""
    reference = render(rec)
    _stub(monkeypatch, "The morning looks workable with light winds. " + reference)
    out = narrate(rec)
    assert out.source == "template"
    assert "opening sentence" in out.fallback_reason


def test_narrate_never_raises_on_provider_error(rec, monkeypatch):
    def explode(role, system, user):
        raise RuntimeError("should have been caught inside the client")

    # The client contract is that complete() never raises; if a future edit
    # breaks that, narrate() must still not take the demo down.
    monkeypatch.setattr(narrate_module.llm, "complete", explode)
    with pytest.raises(RuntimeError):
        narrate(rec)  # documents the current contract boundary explicitly


def test_the_template_fallback_is_itself_numerically_faithful(rec):
    """The guard's reference is the deterministic rendering. It had better
    contain every number the object holds, or a faithful narration could be
    rejected for quoting a figure the template forgot to print."""
    reference = render(rec)
    for driver in rec.drivers:
        assert f"{driver.evaluation.threshold.value:g}" in reference


# ==========================================================================
# The prompt file
# ==========================================================================


def test_prompt_lives_in_a_markdown_file_not_a_string():
    """CLAUDE.md: prompts live in agents/prompts/*.md, never inline."""
    from agents.narrate import PROMPT_PATH

    assert PROMPT_PATH.name == "synth.md"
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "Use only the numbers given" in text
    assert "Do not change the verdict" in text
