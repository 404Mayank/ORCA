"""Tests for the frozen schema package.

These are not coverage tests. Each one pins down a rule from CLAUDE.md or from
``docs/ideal_answers/safety_assess.md`` so that the rule cannot be quietly
removed later by someone who finds a validator inconvenient at 2am.

The negative tests matter more than the positive ones. Anyone can build a valid
object; the question is whether an invalid one is *impossible* to build.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from core.schemas import (
    Assumption,
    Claim,
    ClaimKind,
    Comparison,
    Confidence,
    Driver,
    EvidenceEntry,
    Plan,
    PlanStep,
    QueryType,
    Range,
    Recommendation,
    ResolvedPlace,
    SpatialContext,
    Templated,
    Threshold,
    TurnBackBasis,
    Unit,
    Verdict,
    VerdictValue,
    VesselClass,
    Window,
    evaluate,
    parse_reference,
)
from core.schemas.intent import Intent, QueryType as QT, SpatialReference, TimeWindow
from fixtures.safety_assess_nagapattinam import (
    IST,
    build_degraded_variant,
    build_ideal_safety_answer,
    build_refusal_variant,
)

# ==========================================================================
# The ideal answer round-trips
# ==========================================================================


def test_ideal_answer_builds():
    """The design target is expressible. If this fails, the schema is wrong."""
    rec = build_ideal_safety_answer()
    assert rec.verdict is not None
    assert rec.verdict.value is VerdictValue.MARGINAL
    assert rec.query_type is QueryType.SAFETY_ASSESS


def test_ideal_answer_survives_json_round_trip():
    """Serialisation must not lose anything -- this crosses the API boundary."""
    rec = build_ideal_safety_answer()
    restored = Recommendation.model_validate_json(rec.model_dump_json())
    assert restored == rec


def test_marginal_verdict_is_supported_not_an_afterthought():
    """Requirement: 'marginal' is the common honest answer on this coast."""
    rec = build_ideal_safety_answer()
    # Nothing is breaching, yet the verdict is not a clean go -- that is the
    # entire point of the scenario.
    assert not rec.breaching_drivers()
    assert rec.verdict.value is VerdictValue.MARGINAL


def test_window_carries_three_distinct_times():
    """Requirement 2: turn_back and ashore_by are different instructions."""
    w = build_ideal_safety_answer().window
    assert w.opens < w.turn_back < w.ashore_by
    assert w.workable_hours == pytest.approx(7.0)


def test_turn_back_is_computed_from_steam_time_not_a_flat_margin():
    """The user chose computable. A computed basis must show its inputs."""
    w = build_ideal_safety_answer().window
    assert w.turn_back_basis is TurnBackBasis.COMPUTED_STEAM_TIME
    assert w.steam_distance_km is not None
    assert w.vessel_speed_kn is not None
    # 18 km at 7 kn (12.964 km/h) is about 1.39 h.
    assert w.steam_time_h == pytest.approx(1.39, abs=0.01)


def test_driver_trajectory_holds_both_sides_of_the_threshold():
    """Requirement 4: a driver is under the limit now and over it later."""
    wave = next(d for d in build_ideal_safety_answer().drivers if d.id == "wave_height")
    assert not wave.breaching
    assert wave.trajectory is not None
    assert wave.trajectory.direction == "worsening"
    assert wave.trajectory.breaches_at is not None
    assert wave.trajectory.later.min > wave.evaluation.threshold.value


def test_alternative_carries_a_cost():
    """Requirement 6: 'calmer' is useless; 'calmer but 55 km further' is not."""
    alt = build_ideal_safety_answer().alternatives[0]
    assert alt.cost.extra_distance_km == 55.0
    assert alt.cost.extra_steam_time_h is not None


def test_negative_findings_are_first_class_and_cited():
    """Requirement 5: 'no cyclone' must point at the call that checked."""
    rec = build_ideal_safety_answer()
    ids = {nf.id for nf in rec.negative_findings}
    assert {"no_cyclone", "no_high_wave_warning", "tide_not_limiting"} <= ids
    known = rec.evidence_by_id()
    for nf in rec.negative_findings:
        assert nf.checked_by in known


def test_no_claim_stores_finished_prose():
    """Rule 4: templates and slots, never English sentences.

    Every number a claim mentions must live in `slots`, not baked into the
    template string, or translating the sentence would move the number.
    """
    for claim in build_ideal_safety_answer().claims:
        assert not any(ch.isdigit() for ch in claim.template), (
            f"Claim {claim.id} has a digit in its template: {claim.template!r}. "
            "Numbers belong in slots."
        )


# ==========================================================================
# Degraded and refusal variants
# ==========================================================================


def test_degraded_variant_downgrades_and_drops_unestablished_findings():
    rec = build_degraded_variant()
    assert rec.degraded
    assert rec.verdict.value is VerdictValue.NO_GO
    # The cyclone finding was established by the call that failed.
    assert all(nf.checked_by != "tc_005" for nf in rec.negative_findings)
    assert rec.confidence.overall < build_ideal_safety_answer().confidence.overall


def test_a_failed_safety_check_can_never_produce_a_go():
    """The rule from the ideal answer document, enforced by the type system.

    Built through ``model_validate`` on a plain dict rather than
    ``model_copy``, because ``model_copy`` deliberately skips validators --
    see ``test_model_copy_does_not_skip_validation_in_our_fixtures``.
    """
    payload = build_ideal_safety_answer().model_dump(mode="json")
    for entry in payload["evidence"]:
        if entry["tool_call_id"] == "tc_005":
            entry["status"] = "failed"
            entry["error"] = "timeout after 5s"
    payload["verdict"] = {
        "value": "go",
        "score": 0.2,
        "band": "clear",
        "computed_by": "tc_007",
        "limiting_driver": None,
    }
    with pytest.raises(ValidationError, match="cannot produce a clean go"):
        Recommendation.model_validate(payload)


def test_model_copy_does_not_skip_validation_in_our_fixtures():
    """Guards a real trap.

    ``model_copy(update=...)`` does not re-run validators in Pydantic v2. Any
    fixture that derives one recommendation from another by copying could
    therefore produce an object that the schema would have rejected. The
    degraded builder does exactly that, so it re-validates before returning,
    and this test is what keeps that habit honest.
    """
    degraded = build_degraded_variant()
    # Round-tripping forces every validator to run against the built object.
    Recommendation.model_validate(degraded.model_dump(mode="json"))


def test_verdict_downgrade_is_one_way():
    """A partial answer may get more cautious. It may never get less."""
    v = Verdict(value=VerdictValue.MARGINAL, score=0.6, band="conditional", computed_by="tc_007")
    assert v.downgraded_to(VerdictValue.NO_GO, "x").value is VerdictValue.NO_GO
    # Attempting to "downgrade" to something better is a no-op, not an upgrade.
    assert v.downgraded_to(VerdictValue.GO, "x").value is VerdictValue.MARGINAL


def test_refusal_carries_no_verdict_and_no_drivers():
    rec = build_refusal_variant()
    assert rec.is_refusal()
    assert rec.verdict is None
    assert not rec.drivers and rec.window is None
    assert rec.refusal.supported_region is not None


def test_cannot_both_decide_and_refuse():
    base = build_refusal_variant()
    with pytest.raises(ValidationError, match="both reach a verdict and refuse"):
        Recommendation(
            query_type=QueryType.SAFETY_ASSESS,
            turn_id="t",
            headline=Templated(template="x"),
            refusal=base.refusal,
            verdict=Verdict(value=VerdictValue.GO, score=0.1, band="clear", computed_by="tc_1"),
        )


def test_causal_query_cannot_return_a_safety_verdict():
    """Explaining is not advising."""
    with pytest.raises(ValidationError, match="causal_explain"):
        Recommendation(
            query_type=QueryType.CAUSAL_EXPLAIN,
            turn_id="t",
            headline=Templated(template="x"),
            verdict=Verdict(value=VerdictValue.GO, score=0.1, band="clear", computed_by="tc_1"),
        )


# ==========================================================================
# Rule 3: the evidence link
# ==========================================================================


def test_dangling_evidence_reference_is_rejected_at_construction():
    """A claim pointing at a tool call that does not exist cannot be built."""
    with pytest.raises(ValidationError, match="do not resolve"):
        Recommendation(
            query_type=QueryType.PFZ_LOCATE,
            turn_id="t",
            headline=Templated(template="x"),
            claims=[
                Claim(
                    id="c1",
                    kind=ClaimKind.OBSERVED,
                    template="Chlorophyll is {v} mg/m3.",
                    slots={"v": 0.8},
                    evidence=["tc_does_not_exist"],
                )
            ],
            evidence=[
                EvidenceEntry(tool_call_id="tc_001", tool="chl", source="incois_erddap")
            ],
        )


def test_observed_claim_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="cites no evidence"):
        Claim(id="c1", kind=ClaimKind.OBSERVED, template="Waves are high.")


def test_derived_claim_must_name_its_rule():
    with pytest.raises(ValidationError, match="does not name the rule"):
        Claim(id="c1", kind=ClaimKind.DERIVED, template="Sea is moderate.", evidence=["tc_1"])


def test_inferred_claim_may_be_vague_but_not_numerically_vague():
    """An inferred claim with no numbers is fine..."""
    Claim(id="c1", kind=ClaimKind.INFERRED, template="Fish may aggregate here.")
    # ...but one that names a figure without evidence is how a hallucinated
    # number reaches a fisherman.
    with pytest.raises(ValidationError, match="numerically vague"):
        Claim(
            id="c2",
            kind=ClaimKind.INFERRED,
            template="Skipjack aggregate within {km} km.",
            slots={"km": 12},
        )


def test_inherited_context_must_be_declared_as_an_assumption():
    """Silently reusing a vessel class across turns describes the wrong boat."""
    with pytest.raises(ValidationError, match="no assumption declares it"):
        Recommendation(
            query_type=QueryType.SAFETY_ASSESS,
            turn_id="t_002",
            headline=Templated(template="x"),
            spatial_context=SpatialContext(
                vessel_class=VesselClass.FRP_9M, inherited_from_turn="t_001"
            ),
        )
    # With the assumption declared, it builds.
    Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id="t_002",
        headline=Templated(template="x"),
        spatial_context=SpatialContext(
            vessel_class=VesselClass.FRP_9M, inherited_from_turn="t_001"
        ),
        assumptions=[Assumption(text="Vessel carried over.", source_turn="t_001")],
    )


def test_confidence_cannot_reference_unknown_claims():
    with pytest.raises(ValidationError, match="unknown claim ids"):
        Recommendation(
            query_type=QueryType.PFZ_LOCATE,
            turn_id="t",
            headline=Templated(template="x"),
            claims=[Claim(id="c1", kind=ClaimKind.INFERRED, template="Maybe.")],
            confidence=Confidence(overall=0.5, by_claim={"c9": 0.5}, basis="test"),
        )


def test_unfilled_template_slot_is_caught_not_rendered_literally():
    with pytest.raises(ValueError, match="was not provided"):
        Templated(template="Waves {min}-{max} m", slots={"min": 1.8}).render()


def test_slot_numbers_excludes_booleans():
    """A True must not be offered to the verifier as the number 1."""
    t = Templated(template="{a} {b} {c}", slots={"a": 2.5, "b": True, "c": "x"})
    assert t.slot_numbers() == [2.5]


# ==========================================================================
# Units, ranges and thresholds
# ==========================================================================


def test_range_rejects_backwards_bounds():
    with pytest.raises(ValidationError, match="exceeds max"):
        Range(min=3.0, max=1.0, unit=Unit.METRE)


def test_range_rejects_a_peak_below_max():
    """A gust that is lower than the sustained wind hides the worst case."""
    with pytest.raises(ValidationError, match="below max"):
        Range(min=10.0, max=20.0, unit=Unit.KNOT, peak=15.0)


def test_thresholding_uses_worst_case_not_midpoint():
    """A band whose midpoint is safe can still have an unsafe top end."""
    r = Range(min=2.0, max=2.8, unit=Unit.METRE)  # midpoint 2.4, under 2.5
    t = Threshold(value=2.5, unit=Unit.METRE, comparison=Comparison.LTE, source="test#x")
    assert r.midpoint < t.value
    assert evaluate(r, t).breaching is True


def test_gust_peak_drives_the_breach():
    r = Range(min=15.0, max=20.0, unit=Unit.KNOT, qualifier="gusting 32", peak=32.0)
    t = Threshold(value=25.0, unit=Unit.KNOT, comparison=Comparison.LTE, source="test#x")
    assert evaluate(r, t).breaching is True


def test_unit_mismatch_raises_rather_than_converting_silently():
    """A silent conversion is exactly the bug unit confusion produces."""
    r = Range(min=8.0, max=10.0, unit=Unit.METRE_PER_SECOND)
    t = Threshold(value=25.0, unit=Unit.KNOT, comparison=Comparison.LTE, source="test#x")
    with pytest.raises(ValueError, match="Unit mismatch"):
        evaluate(r, t)


def test_marginal_band_flags_a_near_miss():
    r = Range(min=2.0, max=2.2, unit=Unit.METRE)
    t = Threshold(value=2.5, unit=Unit.METRE, comparison=Comparison.LTE, source="test#x")
    ev = evaluate(r, t)
    assert not ev.breaching and ev.marginal  # 12% headroom, inside the 15% band


def test_gte_threshold_margin_is_signed_the_same_way():
    """Visibility: safe while at or above. Positive margin still means headroom."""
    r = Range(min=6.0, max=8.0, unit=Unit.KILOMETRE)
    t = Threshold(value=2.0, unit=Unit.KILOMETRE, comparison=Comparison.GTE, source="test#x")
    ev = evaluate(r, t)
    assert not ev.breaching and ev.margin > 0


def test_threshold_requires_a_citation():
    with pytest.raises(ValidationError):
        Threshold(value=2.5, unit=Unit.METRE, comparison=Comparison.LTE, source="")


# ==========================================================================
# Intent
# ==========================================================================


def test_safety_query_without_vessel_class_is_a_blocking_gap():
    """The governing rule: never guess a vessel class for a safety question."""
    intent = Intent(
        query_type=QT.SAFETY_ASSESS,
        raw_query="is it safe tomorrow?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
    )
    assert "vessel_class" in intent.blocking_gaps()


def test_pfz_query_without_vessel_class_is_answerable():
    """Only safety verdicts have per-vessel thresholds, so only they block."""
    intent = Intent(
        query_type=QT.PFZ_LOCATE,
        raw_query="where are the fish?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
    )
    assert intent.blocking_gaps() == []


def test_time_window_rejects_naive_datetimes():
    with pytest.raises(ValidationError, match="timezone-aware"):
        TimeWindow(start=datetime(2025, 11, 15, 4), end=datetime(2025, 11, 15, 12))


def test_spatial_reference_needs_a_name_or_a_coordinate():
    with pytest.raises(ValidationError, match="name or a coordinate"):
        SpatialReference()


def test_spatial_reference_rejects_half_a_coordinate():
    with pytest.raises(ValidationError, match="together or not at all"):
        SpatialReference(name="x", lat=10.0)


# ==========================================================================
# Plan
# ==========================================================================


def _step(sid: str, tool: str = "noop", **kw) -> PlanStep:
    return PlanStep(id=sid, tool=tool, **kw)


def test_plan_detects_a_cycle_and_names_it():
    with pytest.raises(ValidationError, match="cycle"):
        Plan(
            intent_type=QueryType.SAFETY_ASSESS,
            steps=[
                _step("s1", depends_on=["s2"]),
                _step("s2", depends_on=["s1"]),
            ],
        )


def test_plan_rejects_a_reference_not_declared_as_a_dependency():
    """Data flow and declared ordering must agree, or the executor reads a null."""
    with pytest.raises(ValidationError, match="does not declare them in depends_on"):
        Plan(
            intent_type=QueryType.SAFETY_ASSESS,
            steps=[_step("s1"), _step("s2", args={"wave": "$s1"})],
        )


def test_plan_rejects_unknown_step_references():
    with pytest.raises(ValidationError, match="unknown steps"):
        Plan(
            intent_type=QueryType.SAFETY_ASSESS,
            steps=[_step("s1", args={"x": "$s9"}, depends_on=["s9"])],
        )


def test_plan_rejects_duplicate_step_ids():
    with pytest.raises(ValidationError, match="Duplicate step ids"):
        Plan(intent_type=QueryType.SAFETY_ASSESS, steps=[_step("s1"), _step("s1")])


def test_plan_enforces_the_step_cap():
    with pytest.raises(ValidationError, match="over the cap"):
        Plan(
            intent_type=QueryType.SAFETY_ASSESS,
            steps=[_step(f"s{i}") for i in range(1, 15)],
        )


def test_malformed_reference_is_caught_not_passed_through_as_a_string():
    with pytest.raises(ValidationError, match="looks like a reference"):
        _step("s1", args={"x": "$s3."})


def test_reference_parsing():
    assert parse_reference("$s3") == ("s3", [])
    assert parse_reference("$s3.stats.mean") == ("s3", ["stats", "mean"])
    assert parse_reference("Nagapattinam") is None
    assert parse_reference("$bad") is None


def test_execution_layers_group_parallel_work():
    """The four agent groups must be able to run concurrently where possible."""
    plan = Plan(
        intent_type=QueryType.SAFETY_ASSESS,
        steps=[
            _step("s1", tool="resolve_place"),
            _step("s2", tool="wave_forecast", args={"p": "$s1"}, depends_on=["s1"]),
            _step("s3", tool="wind_forecast", args={"p": "$s1"}, depends_on=["s1"]),
            _step(
                "s4",
                tool="compute_risk_score",
                args={"w": "$s2", "v": "$s3"},
                depends_on=["s2", "s3"],
            ),
        ],
    )
    assert plan.execution_layers() == [["s1"], ["s2", "s3"], ["s4"]]


def test_nested_references_are_found():
    """A reference can sit inside a list or a nested object."""
    step = _step("s5", args={"points": [{"a": "$s1"}, {"b": "$s2.lat"}]}, depends_on=["s1", "s2"])
    assert step.referenced_steps() == {"s1", "s2"}


# ==========================================================================
# Window validation
# ==========================================================================


def test_window_rejects_out_of_order_times():
    with pytest.raises(ValidationError, match="opens < turn_back <= ashore_by"):
        Window(
            opens=datetime(2025, 11, 15, 12, tzinfo=IST),
            turn_back=datetime(2025, 11, 15, 11, tzinfo=IST),
            ashore_by=datetime(2025, 11, 15, 13, tzinfo=IST),
            basis="tc_1",
            turn_back_basis=TurnBackBasis.FLAT_MARGIN,
        )


def test_computed_turn_back_must_show_its_inputs():
    """Claiming a computation that did not happen is worse than a flat margin."""
    with pytest.raises(ValidationError, match="would make it defensible are missing"):
        Window(
            opens=datetime(2025, 11, 15, 4, tzinfo=IST),
            turn_back=datetime(2025, 11, 15, 11, tzinfo=IST),
            ashore_by=datetime(2025, 11, 15, 13, tzinfo=IST),
            basis="tc_1",
            turn_back_basis=TurnBackBasis.COMPUTED_STEAM_TIME,
        )
