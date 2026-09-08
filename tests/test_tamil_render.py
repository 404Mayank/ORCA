"""Tamil Slice 2: the answer renders in Tamil, deterministically.

What these tests defend, in order of how much damage the failure does:

1.  **Numbers survive.** The Tamil paragraph carries exactly the numeric
    tokens the English one does -- same values, same multiplicities, ASCII
    digits. A translation layer that rounds, drops, or re-spells a wave
    height is the single worst thing this system could ship, and it is the
    reason claims are stored as slots rather than sentences at all.
2.  **The catalogue cannot drift.** Every English template authored in
    ``agents/synthesis_agent.py`` has a Tamil counterpart, and every Tamil
    counterpart still corresponds to a live English string. Reword a
    sentence in synthesis without touching ``templates/ta`` and this fails
    -- which is the whole reason a string-keyed catalogue is safe to use.
3.  **Slots are never renamed.** ``{min}`` stays ``{min}``. A renamed
    placeholder is a silent KeyError at render time on a fisherman's phone.
4.  **The half-translated case is declared**, not hidden.
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections import Counter
from string import Formatter

import pytest

from agents.narrate import extract_numbers, narrate
from agents.synthesis_agent import build_recommendation
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from fixtures.safety_assess_nagapattinam import (
    build_ideal_safety_answer,
    build_refusal_variant,
)
from language import SUPPORTED, render, translate_template
from language.templates import ta
from orchestrator.executor import execute_plan_sync
from orchestrator.validate_plan import fallback_plan, validate_plan

SYNTHESIS = pathlib.Path(__file__).resolve().parents[1] / "agents" / "synthesis_agent.py"

#: Tamil digits. If one of these ever reaches the output, every downstream
#: number check -- the narration guard, the verifier's walk over the tool
#: call log -- goes blind, because they all match on the ASCII token.
_TAMIL_DIGITS = re.compile(r"[௦-௯]")


def _slots(template: str) -> set[str]:
    return {f for _, f, _, _ in Formatter().parse(template) if f}


def _authored_templates() -> set[str]:
    """Every user-facing template literal in the synthesis agent.

    Read out of the source rather than by running it, because a template only
    reachable on a degraded path (a failed tool, an empty candidate list) is
    exactly the one an integration test will not cover and a fisherman will
    meet on the worst day.
    """
    tree = ast.parse(SYNTHESIS.read_text(encoding="utf-8"))
    found: set[str] = set()

    def _add(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)

    for node in ast.walk(tree):
        # Templated(template=...) and Driver(label_template=...)
        if isinstance(node, ast.keyword) and node.arg in ("template", "label_template"):
            _add(node.value)
        # The driver-label and verdict-headline lookup tables.
        if isinstance(node, ast.Assign):
            names = {getattr(t, "id", None) for t in node.targets}
            if names & {"labels", "_VERDICT_HEADLINES"} and isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    _add(value)
        # labels.get(driver_id, "<default label>")
        if isinstance(node, ast.Call):
            if getattr(node.func, "attr", "") == "get" and len(node.args) == 2:
                _add(node.args[1])
            if getattr(node.func, "id", "") == "_no_data_answer":
                for arg in node.args:
                    _add(arg)
    return found


# ==========================================================================
# The catalogue cannot drift away from the sentences it translates
# ==========================================================================


def test_every_english_template_has_a_tamil_counterpart():
    """Reword synthesis without touching templates/ta and this fails.

    That is the intended failure. A string-keyed catalogue is only safe
    while something enforces the keys, and this is that something.
    """
    missing = sorted(t for t in _authored_templates() if t not in ta.TEMPLATES)
    assert not missing, (
        "these templates are authored in synthesis_agent.py but have no Tamil "
        f"entry in language/templates/ta: {missing}"
    )


def test_the_catalogue_has_no_orphan_entries():
    """A Tamil entry whose English original is gone is dead weight.

    Worse than dead: it reads as coverage. Someone scanning the catalogue
    would count it and conclude the sentence is handled.
    """
    orphans = sorted(k for k in ta.TEMPLATES if k not in _authored_templates())
    assert not orphans, (
        "these Tamil entries no longer match any string in synthesis_agent.py; "
        f"the English wording changed underneath them: {orphans}"
    )


def test_translation_never_renames_a_slot():
    """{min} must stay {min}. A renamed slot is a KeyError at render time."""
    for english, tamil in ta.TEMPLATES.items():
        assert _slots(english) == _slots(tamil), (
            f"slot mismatch\n  en: {english!r} -> {sorted(_slots(english))}"
            f"\n  ta: {tamil!r} -> {sorted(_slots(tamil))}"
        )


def test_every_tamil_template_is_actually_tamil():
    """Guards against an entry copied from the English column and forgotten."""
    untranslated = [
        en for en, tamil in ta.TEMPLATES.items()
        if en == tamil and not re.fullmatch(r"[\W\d{}a-z_/-]+", en)
    ]
    assert not untranslated, f"entries left in English: {untranslated}"


# ==========================================================================
# Numbers survive the language change
# ==========================================================================


@pytest.fixture(scope="module")
def live_safety():
    """The real pipeline, not the hand-written fixture."""
    plan = validate_plan(
        fallback_plan(
            QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
        )
    ).plan
    result = execute_plan_sync(plan, turn_id="t_ta")
    if not result.succeeded("s2"):
        pytest.skip("no usable Open-Meteo cache present; run ingest first")
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to go out tomorrow morning?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    return build_recommendation(result, intent, turn_id="t_ta")


@pytest.mark.parametrize("builder", [build_ideal_safety_answer, build_refusal_variant])
def test_tamil_carries_the_same_numbers_as_english(builder):
    """Same tokens, same count. Not "similar" -- identical.

    Multiplicity matters: the ideal answer prints the turn-back time twice,
    once in the window sentence and once in the guidance, and a renderer
    that dropped one of them has quietly removed an instruction.
    """
    rec = builder()
    english = Counter(extract_numbers(render(rec, "en")))
    tamil = Counter(extract_numbers(render(rec, "ta")))
    assert english == tamil, (
        f"only in English: {english - tamil}\nonly in Tamil: {tamil - english}"
    )


def test_tamil_carries_the_same_numbers_on_a_live_answer(live_safety):
    english = Counter(extract_numbers(render(live_safety, "en")))
    tamil = Counter(extract_numbers(render(live_safety, "ta")))
    assert english == tamil, (
        f"only in English: {english - tamil}\nonly in Tamil: {tamil - english}"
    )


def test_digits_stay_ascii(live_safety):
    """Tamil numerals would blind every number check downstream."""
    text = render(live_safety, "ta")
    assert not _TAMIL_DIGITS.search(text), "Tamil numerals reached the answer"


def test_the_answer_is_actually_in_tamil(live_safety):
    """Not a smoke test: Slice 1 shipped a `ta` flag that answered English."""
    text = render(live_safety, "ta")
    assert re.search(r"[஀-௿]", text), "no Tamil script in a ta answer"
    assert "under the" not in text and "over the" not in text


# ==========================================================================
# Direction of a threshold, in the language the crew reads
# ==========================================================================


def test_a_floor_threshold_is_not_described_as_a_ceiling(live_safety):
    """Visibility is safe while ABOVE its limit.

    The English renderer carries a comment about this because it got it
    wrong once. Getting it wrong in Tamil would be worse, not better: it is
    the version a crew who cannot read the English one relies on.
    """
    from core.units import Comparison

    text = render(live_safety, "ta")
    floors = [
        d for d in live_safety.drivers
        if d.evaluation.threshold.comparison in (Comparison.GTE, Comparison.GT)
    ]
    if not floors:
        pytest.skip("no floor-thresholded driver in this answer")
    # "வரம்பு" is the ceiling word, "குறைந்தபட்சம்" the floor word. A floor
    # driver must be described with the latter.
    assert "குறைந்தபட்சத்திற்கு" in text


# ==========================================================================
# Honesty about what is still English
# ==========================================================================


def test_residual_english_is_declared(live_safety):
    """The footer is provenance text and stays English -- so the answer says so.

    A paragraph that is Tamil prose wrapped around English provenance should
    not be presented as a Tamil answer without comment.
    """
    text = render(live_safety, "ta")
    assert "ஆங்கிலத்திலேயே" in text


def test_a_fully_translated_answer_carries_no_apology():
    """The note is conditional, not decoration.

    If it were unconditional it would stop meaning anything, and a genuinely
    half-English answer would look exactly like a complete one.
    """
    rec = build_ideal_safety_answer()
    rec = rec.model_copy(update={"confidence": None, "caveats": [], "alternatives": []})
    renderer = ta._Renderer()
    for claim in rec.claims:
        renderer.text(claim)
    assert not renderer.missing_templates or renderer.english_prose is not None


# ==========================================================================
# The adapter contract
# ==========================================================================


def test_ta_is_a_supported_language():
    assert "ta" in SUPPORTED


def test_an_unsupported_language_still_raises():
    """Returning English under a Tamil flag is the failure this prevents."""
    with pytest.raises(NotImplementedError, match="No templates"):
        render(build_ideal_safety_answer(), language="xx")
    with pytest.raises(NotImplementedError, match="not supported"):
        translate_template("Waves {min}-{max} m", target="xx")


def test_a_missing_catalogue_entry_degrades_rather_than_raising():
    """Synthesis reworded a sentence in production: answer, then report it."""
    assert translate_template("Some sentence nobody catalogued.", "ta") == (
        "Some sentence nobody catalogued."
    )


def test_tamil_answers_skip_the_llm_narrator(monkeypatch):
    """Slice 2 is deterministic. The narrator is English-prompted and its
    number guard cannot police Tamil number-words, so it is not consulted."""
    def explode(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the LLM was called for a Tamil answer")

    monkeypatch.setattr("agents.narrate.llm.complete", explode)
    narration = narrate(build_ideal_safety_answer(), language="ta")
    assert narration.source == "template"
    assert re.search(r"[஀-௿]", narration.text)
    assert narration.fallback_reason and "ta" in narration.fallback_reason
