"""Tests for the verifier -- ORCA's headline technical claim.

CLAUDE.md requires a test that feeds the verifier a hallucinated number and
asserts rejection. That is ``test_hallucinated_wave_height_is_rejected``, and
the rest of this file exists so that test cannot pass trivially: a verifier
that rejected everything would also pass it. So the positive cases matter as
much as the negative ones, and the rounding tests pin down the boundary
between "display-rounded" and "invented".
"""

from __future__ import annotations

import pytest

from core.schemas import Claim, ClaimKind, Recommendation, Verdict, VerdictValue
from core.schemas.tool_io import ToolCallRecord, ToolStatus
from fixtures.safety_assess_nagapattinam import (
    build_degraded_variant,
    build_failed_alert_log,
    build_ideal_safety_answer,
    build_refusal_variant,
    build_tool_call_log,
)
from orchestrator.verifier import Severity, numbers_match, verify


def _rebuild(rec: Recommendation, **updates) -> Recommendation:
    """Re-validate after a copy, since model_copy skips validators."""
    return Recommendation.model_validate(rec.model_copy(update=updates).model_dump())


# ==========================================================================
# The mandated test
# ==========================================================================


def test_hallucinated_wave_height_is_rejected():
    """THE test. A number that no tool produced must not survive verification.

    The tool returned a wave height that rounds to 2.2 m. The claim is edited
    to say 3.4 m -- a plausible-looking figure of the right shape and unit,
    which is exactly what a hallucination looks like. Nothing about the
    document's structure changes; only the number is wrong.
    """
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()

    assert verify(rec, log).ok, "precondition: the honest answer must verify"

    tampered = _rebuild(
        rec,
        claims=[
            c.model_copy(update={"slots": {**c.slots, "max": 3.4}}) if c.id == "c1" else c
            for c in rec.claims
        ],
    )

    report = verify(tampered, log)
    assert not report.ok
    codes = {v.code for v in report.errors}
    assert "unsupported_number" in codes
    assert any("3.4" in v.detail for v in report.errors)


def test_hallucinated_verdict_score_is_rejected():
    """The single number the governing rule exists to protect."""
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()

    tampered = _rebuild(
        rec,
        verdict=rec.verdict.model_copy(update={"score": 0.11, "value": VerdictValue.MARGINAL}),
    )
    report = verify(tampered, log)
    assert not report.ok
    assert "unsupported_verdict_score" in {v.code for v in report.errors}


# ==========================================================================
# The honest answer passes
# ==========================================================================


def test_ideal_answer_verifies():
    report = verify(build_ideal_safety_answer(), build_tool_call_log())
    assert report.ok, [str(v) for v in report.violations]
    assert report.numbers_checked > 20, "a verifier that checks nothing passes everything"
    assert report.claims_checked == 4


def test_refusal_verifies():
    """A refusal makes no numeric claims, so it has nothing to fail on."""
    rec = build_refusal_variant()
    log = {
        "tc_001": ToolCallRecord(
            tool_call_id="tc_001",
            tool="resolve_place",
            output_numbers=[9.93, 76.27],
            status=ToolStatus.OK,
        )
    }
    assert verify(rec, log).ok


def test_degraded_variant_verifies_against_its_own_log():
    """Degraded is not the same as invalid. The answer is honest, just weaker."""
    report = verify(build_degraded_variant(), build_failed_alert_log())
    assert report.ok, [str(v) for v in report.violations]


# ==========================================================================
# Rounding: the boundary between display and invention
# ==========================================================================


def test_display_rounding_is_accepted():
    """A tool returns 2.17; the claim says 2.2. That is reporting, not inventing."""
    assert numbers_match(2.2, 2.17)
    assert numbers_match(1.8, 1.82)
    assert numbers_match(15, 15.2)


def test_rounding_does_not_stretch_to_a_different_number():
    assert not numbers_match(2.2, 2.7)
    assert not numbers_match(2.2, 2.26)
    assert not numbers_match(3.4, 2.17)


def test_precision_of_the_claim_sets_the_tolerance():
    """A vaguer claim gets a wider window -- and says less. A precise claim
    gets a narrow one. The claim cannot buy slack without losing content."""
    assert numbers_match(2, 2.4)  # "about 2 m" tolerates 2.4
    assert not numbers_match(2.5, 2.44)  # one decimal place is a tight window
    assert not numbers_match(2.55, 2.6)  # two decimals, tighter still


def test_trailing_zeros_cannot_tighten_the_window():
    """A known and unavoidable limitation, pinned so nobody assumes otherwise.

    Python cannot distinguish the float 2.0 from the float 2 -- the authored
    precision is gone by the time the value reaches the verifier. So "2.0 m"
    is checked at whole-number tolerance, not at one decimal place.

    This only ever makes the verifier more permissive, never less, and it can
    be avoided in practice by not writing trailing zeros into a slot. Fixing
    it properly would mean storing claim values as strings or Decimals, which
    is a real cost for a case that does not arise in generated output.
    """
    assert numbers_match(2.0, 2.4)
    assert numbers_match(2, 2.4)  # identical treatment, by necessity


# ==========================================================================
# Provenance attacks
# ==========================================================================


def test_fabricated_evidence_digest_is_rejected():
    """The reason the verifier checks the LOG, not the answer's own evidence.

    An agent that invents a number could also invent the evidence entry that
    appears to support it. Verifying a document against its own citations
    proves nothing, so the digest is checked against what the tool returned.
    """
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()

    tampered = _rebuild(
        rec,
        evidence=[
            e.model_copy(update={"output_digest": {**e.output_digest, "hs_max_m": 3.4}})
            if e.tool_call_id == "tc_003"
            else e
            for e in rec.evidence
        ],
    )
    report = verify(tampered, log)
    assert not report.ok
    assert "fabricated_evidence_value" in {v.code for v in report.errors}


def test_citing_a_call_that_never_ran_is_rejected():
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    del log["tc_003"]
    report = verify(rec, log)
    assert not report.ok
    assert "missing_tool_call" in {v.code for v in report.errors}


def test_verdict_must_come_from_compute_risk_score():
    """A safety verdict sourced from a wave forecast is not a verdict."""
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    log["tc_007"] = log["tc_007"].model_copy(update={"tool": "wave_forecast"})
    report = verify(rec, log)
    assert not report.ok
    assert "verdict_from_wrong_tool" in {v.code for v in report.errors}


def test_invented_coordinate_is_rejected():
    """The LLM may say 'Nagapattinam'. It may never produce the coordinate."""
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    log["tc_001"] = log["tc_001"].model_copy(update={"output_numbers": [11.90, 79.84]})
    report = verify(rec, log)
    assert not report.ok
    assert "invented_coordinate" in {v.code for v in report.errors}


def test_negative_finding_resting_on_a_failed_check_is_rejected():
    """'No cyclone active' after the cyclone check timed out is not a finding.

    This is the difference between "we looked and there is nothing" and "we
    could not look". Reporting the second as the first is the most dangerous
    single failure this system could have.
    """
    rec = build_ideal_safety_answer()  # still asserts no_cyclone via tc_005
    log = build_failed_alert_log()  # but tc_005 failed
    report = verify(rec, log)
    assert not report.ok
    assert "negative_finding_from_failed_check" in {v.code for v in report.errors}


def test_claim_citing_a_failed_call_is_rejected():
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    log["tc_003"] = log["tc_003"].model_copy(
        update={"status": ToolStatus.FAILED, "error": "upstream 503"}
    )
    report = verify(rec, log)
    assert not report.ok
    assert "claim_cites_failed_call" in {v.code for v in report.errors}


def test_driver_observed_values_must_be_real():
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    log["tc_004"] = log["tc_004"].model_copy(update={"output_numbers": [1.0, 2.0]})
    report = verify(rec, log)
    assert not report.ok
    assert "unsupported_driver_value" in {v.code for v in report.errors}


# ==========================================================================
# Strictness by claim kind
# ==========================================================================


def test_inferred_claims_warn_rather_than_reject():
    """Stricter for observed than inferred, per CLAUDE.md.

    A soft number on the LLM's own hypothesis is a warning. The same number on
    an observed claim rejects the answer. Both are reported; only one stops it.
    """
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()

    inferred = _rebuild(
        rec,
        claims=[
            c.model_copy(
                update={
                    "template": "Fish likely within {km} km.",
                    "slots": {"km": 999},
                }
            )
            if c.id == "c4"
            else c
            for c in rec.claims
        ],
    )
    report = verify(inferred, log)
    assert report.ok, "an inferred claim must not reject the whole answer"
    assert any(v.severity is Severity.WARNING and v.code == "unsupported_number" for v in report.violations)


def test_derived_claims_are_held_to_the_strict_standard():
    """A derived number comes from a named rule over tool outputs. If it traces
    to nothing, the rule did not run and something else produced the figure."""
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()

    tampered = _rebuild(
        rec,
        claims=[
            c.model_copy(
                update={
                    "template": "Sea state index is {idx} for a {vessel}.",
                    "slots": {"idx": 42.0, "vessel": "9 m FRP boat"},
                }
            )
            if c.id == "c2"
            else c
            for c in rec.claims
        ],
    )
    report = verify(tampered, log)
    assert not report.ok
    assert any(v.severity is Severity.ERROR for v in report.errors)


def test_report_produces_actionable_revision_feedback():
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    tampered = _rebuild(
        rec,
        claims=[
            c.model_copy(update={"slots": {**c.slots, "max": 9.9}}) if c.id == "c1" else c
            for c in rec.claims
        ],
    )
    feedback = verify(tampered, log).revision_feedback()
    assert "claim[c1]" in feedback
    assert "9.9" in feedback


def test_alternatives_are_verified_too():
    """The Palk Bay numbers are a safety claim about a different place."""
    rec = build_ideal_safety_answer()
    log = build_tool_call_log()
    log["tc_008"] = log["tc_008"].model_copy(update={"output_numbers": [55.0]})
    report = verify(rec, log)
    assert not report.ok
    assert any(v.where.startswith("alternative[") for v in report.errors)
