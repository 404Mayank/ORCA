"""Tests for recommendation assembly and English rendering.

The integration tests at the bottom are the ones that matter most: they run
the whole pipeline -- plan, execute, synthesise, verify, render -- and assert
that the verifier passes. A synthesis layer that quietly invented a number
would fail there, which is the point of building the verifier first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.synthesis_agent import KM_PER_KNOT_HOUR, build_recommendation
from core import config
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from core.schemas.recommendation import VerdictValue
from language import detect_language, render, translate_template
from language.templates.en import render_headline
from orchestrator.executor import execute_plan_sync
from orchestrator.validate_plan import fallback_plan, validate_plan
from orchestrator.verifier import verify

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def pipeline():
    """Run the real safety_assess pipeline once and share the result."""
    plan = validate_plan(
        fallback_plan(
            QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
        )
    ).plan
    result = execute_plan_sync(plan, turn_id="t_test")
    if not result.succeeded("s2"):
        pytest.skip("no usable Open-Meteo cache present; run ingest first")
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to go out tomorrow morning?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    rec = build_recommendation(result, intent, turn_id="t_test")
    return result, rec


# ==========================================================================
# The whole pipeline
# ==========================================================================


def test_the_assembled_answer_passes_the_verifier(pipeline):
    """The headline claim, exercised on a real run rather than a fixture.

    Synthesis builds the object; the verifier independently checks every
    number in it against the executor's tool call log. If synthesis ever
    rounds wrongly, copies from the wrong step, or invents a figure, this
    fails.
    """
    result, rec = pipeline
    report = verify(rec, result.tool_call_log)
    assert report.ok, [str(v) for v in report.violations]
    assert report.numbers_checked > 20


def test_the_answer_renders_to_english(pipeline):
    _, rec = pipeline
    text = render(rec)
    assert len(text) > 100
    assert "{" not in text, "an unfilled slot reached the reader"


def test_every_driver_is_printed_with_the_limit_it_is_judged_against(pipeline):
    """'2.2 m' is a fact. '2.2 m against a 2.5 m limit' is an argument."""
    _, rec = pipeline
    text = render(rec)
    for driver in rec.drivers:
        threshold = driver.evaluation.threshold
        assert f"{threshold.value:g}" in text, f"{driver.id} printed without its limit"


def test_a_floor_threshold_is_described_as_a_minimum_not_a_limit(pipeline):
    """Regression test for a real wording bug.

    Visibility is safe while ABOVE its threshold. An earlier renderer said
    "24 km, under the 2 km limit", which tells a fisherman the opposite of
    what the check actually found.
    """
    _, rec = pipeline
    if not any(d.id == "visibility" for d in rec.drivers):
        pytest.skip("no visibility driver in this run")
    text = render(rec)
    assert "above the 2 km minimum" in text
    assert "under the 2 km limit" not in text


def test_the_reason_for_a_downgrade_outranks_the_timing_advice(pipeline):
    """A fisherman needs to know WHY he is being told to stay in first."""
    _, rec = pipeline
    if not any("downgraded" in g.template.lower() for g in rec.operational_guidance):
        pytest.skip("this run was not downgraded; degraded without a downgrade reason is a different case")
    top = min(rec.operational_guidance, key=lambda g: g.priority)
    assert "downgraded" in top.template.lower()


def test_a_failed_alert_check_produces_no_cyclone_negative_finding(pipeline):
    """'We could not check' must not become 'there is no cyclone'.

    If synthesis emitted the finding anyway, the verifier would reject the
    answer -- so this asserts the two layers agree rather than merely that one
    of them works.
    """
    result, rec = pipeline
    alerts = result.output_for("s5")
    if alerts is not None and alerts.status.value == "ok":
        pytest.skip("alert source is wired; this test covers the failed case")
    assert not any(f.id == "no_cyclone" for f in rec.negative_findings)
    assert verify(rec, result.tool_call_log).ok


def test_evidence_is_built_from_the_log_so_the_two_cannot_disagree(pipeline):
    result, rec = pipeline
    assert {e.tool_call_id for e in rec.evidence} == set(result.tool_call_log)


def test_the_trace_records_every_step_whatever_its_status(pipeline):
    """Nothing is dropped from the trace.

    This used to assert that at least one step had *failed*, which was only
    reliably true while the alert source was unwired. Once GDACS landed on
    2026-09-06 a clean run had nothing failing and the test broke, having been
    a test of the world rather than of the code. The invariant is that the
    trace mirrors what ran -- successes and failures alike.
    """
    result, rec = pipeline
    assert len(rec.reasoning_trace) == len(result.trace)
    assert [s.status for s in rec.reasoning_trace] == [s.status for s in result.trace]


def test_a_failed_step_reaches_the_trace(no_alert_sources):
    """The half of the above that needs a failure, with one created on purpose."""
    plan = validate_plan(
        fallback_plan(
            QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
        )
    ).plan
    result = execute_plan_sync(plan, turn_id="t_failed")
    if not result.succeeded("s2"):
        pytest.skip("no usable Open-Meteo cache present; run ingest first")
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to go out tomorrow morning?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    rec = build_recommendation(result, intent, turn_id="t_failed")

    assert any(step.status == "failed" for step in rec.reasoning_trace)
    # And the answer is still verifiable -- degrading is not the same as
    # falling over.
    assert verify(rec, result.tool_call_log).ok


# ==========================================================================
# The window
# ==========================================================================


def test_turn_back_is_computed_from_steam_time(pipeline):
    """The user asked for a computable turn-back rather than a flat margin."""
    _, rec = pipeline
    if rec.window is None:
        pytest.skip("conditions never breach in this forecast; no window to close")
    window = rec.window
    assert window.turn_back_basis.value == "computed_steam_time"

    spec = config.vessel_spec("frp_9m")
    expected_h = spec["typical_operating_range_km"] / (
        spec["cruise_speed_kn"] * KM_PER_KNOT_HOUR
    )
    assert window.steam_time_h == pytest.approx(expected_h, abs=0.01)
    # turn_back must be exactly one steam time before the deadline.
    gap = (window.ashore_by - window.turn_back).total_seconds() / 3600.0
    assert gap == pytest.approx(expected_h, abs=0.02)


def test_a_computed_window_declares_its_provisional_inputs(pipeline):
    """Operating range and cruise speed are tagged provisional in config, and
    this number decides how early a man is told to start for home. The
    assumption must be visible, not buried."""
    _, rec = pipeline
    if rec.window is None:
        pytest.skip("no window in this run")
    assert any("provisional" in a.text.lower() for a in rec.assumptions)


def test_no_window_is_offered_when_conditions_never_breach(pipeline):
    """Silence beats a fabricated deadline.

    If the sea stays inside limits for the whole forecast there is no
    turn-back time to give, and inventing one would be worse than omitting it.
    """
    _, rec = pipeline
    wave = rec.drivers[0] if rec.drivers else None
    if wave and wave.trajectory and wave.trajectory.breaches_at:
        pytest.skip("conditions do breach in this run")
    assert rec.window is None


# ==========================================================================
# The language adapter
# ==========================================================================


def test_language_detection_is_an_honest_stub():
    assert detect_language("is it safe to go out?") == "en"
    assert detect_language("anything at all") == "en"


def test_translation_refuses_an_unsupported_language():
    """Better a clear failure than silently returning English as Tamil."""
    with pytest.raises(NotImplementedError, match="not supported"):
        translate_template("Waves {min}-{max} m", target="ta")


def test_translation_preserves_slot_placeholders():
    """A translator that renamed {min} would break every claim silently."""
    template = "Waves {min}-{max} {unit}"
    assert "{min}" in translate_template(template)
    assert "{max}" in translate_template(template)


def test_rendering_an_unsupported_language_fails_loudly(pipeline):
    _, rec = pipeline
    with pytest.raises(NotImplementedError):
        render(rec, language="ta")


def test_headline_reflects_the_verdict(pipeline):
    _, rec = pipeline
    headline = render_headline(rec)
    if rec.verdict.value is VerdictValue.NO_GO:
        assert "not go" in headline.lower()
    elif rec.verdict.value is VerdictValue.MARGINAL:
        assert "marginal" in headline.lower()


def test_the_risk_score_never_appears_in_the_spoken_answer(pipeline):
    """'0.1452' means nothing to a fisherman.

    The score drives the verdict internally and belongs in the evidence
    drawer, not in the sentence a man reads before deciding to launch.
    """
    _, rec = pipeline
    text = render(rec)
    assert f"{rec.verdict.score}" not in text


def test_the_answer_always_states_what_it_is_based_on(pipeline):
    """confidence.basis is required, and it must reach the reader."""
    _, rec = pipeline
    assert "Based on:" in render(rec)


# ==========================================================================
# Guards
# ==========================================================================


def test_synthesis_says_so_rather_than_raising_when_the_data_is_missing():
    """Every query type now assembles. With no data it must still answer.

    This used to assert NotImplementedError for causal_explain, which was
    correct while only safety_assess was built. The three other builders landed
    on 2026-09-06, so the invariant moved: an empty run produces a well-formed
    answer that says the data was not there, keeping its evidence and trace,
    rather than a stack trace a judge would see mid-demo.
    """
    from orchestrator.executor import ExecutionResult
    from core.provenance import ToolCallLog

    empty = ExecutionResult(log=ToolCallLog("t_x"))
    intent = Intent(query_type=QueryType.CAUSAL_EXPLAIN, raw_query="why fewer fish?")
    rec = build_recommendation(empty, intent)
    assert rec.degraded
    assert rec.verdict is None
    assert "could not" in rec.headline.template.lower()


@pytest.mark.parametrize(
    "query_type",
    [QueryType.PFZ_LOCATE, QueryType.GEOFENCE_CHECK, QueryType.CAUSAL_EXPLAIN],
)
def test_only_safety_assess_ever_carries_a_verdict(query_type):
    """A verdict is a go/no-go safety adjudication, and compute_risk_score is
    its only legitimate source. Attaching one to "where are the fish" would
    smuggle a safety claim into an answer that never assessed safety."""
    from orchestrator.executor import ExecutionResult
    from core.provenance import ToolCallLog

    empty = ExecutionResult(log=ToolCallLog("t_x"))
    rec = build_recommendation(empty, Intent(query_type=query_type, raw_query="q"))
    assert rec.verdict is None


def test_a_missing_risk_result_produces_an_honest_non_answer():
    """No verdict is better than an invented one."""
    from core.provenance import ToolCallLog
    from orchestrator.executor import ExecutionResult

    empty = ExecutionResult(log=ToolCallLog("t_x"), degraded=True)
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="safe?",
        vessel_class=VesselClass.FRP_9M,
    )
    rec = build_recommendation(empty, intent)
    assert rec.verdict is None
    assert rec.degraded
    assert rec.confidence.overall == 0.0
    assert "did not run" in rec.confidence.basis
