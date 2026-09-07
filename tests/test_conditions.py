"""Conditions report: read-only tide/weather/alerts answers, and the fence.

The type exists so "what are conditions near me" stops being forced through
the safety gate (which demands a vessel the user never came for). The fence
exists so safety phrasing can never be answered vessel-free: any query at
safety strength routes to SAFETY_ASSESS at any margin, in code, on both the
keyword tier (classify) and the LLM tier (planner gate).
"""

from __future__ import annotations

from datetime import datetime, timezone

from agents import keyword_intent
from agents.intent_planner_agent import _finish, _use_fallback, parse_planner_json
from agents.synthesis_agent import build_recommendation
from core.provenance import ToolCallLog
from core.schemas.intent import Intent, PlannerOutput, QueryType, SpatialReference
from core.schemas.tool_io import Provenance, ToolStatus
from core.units import Range, Unit
from language.templates.en import render
from orchestrator.executor import ExecutionResult
from orchestrator.llm.client import LLMResult
from orchestrator.validate_plan import fallback_plan, validate_plan
from orchestrator.verifier import verify
from tools.geo.nearest import ResolvePlaceOut
from tools.weather.alerts import ActiveAlertsOut
from tools.weather.tides import TideExtreme, TidesOut
from tools.weather.wave_forecast import WaveForecastOut
from tools.weather.wind_forecast import WindForecastOut


def _intent(query_type=QueryType.CONDITIONS_REPORT, place="Nagapattinam"):
    return Intent(
        query_type=query_type,
        raw_query="test",
        spatial_reference=SpatialReference(name=place) if place else None,
    )


def _result() -> ExecutionResult:
    """Five OK steps with the fallback plan's numbering (s1..s5)."""
    log = ToolCallLog(turn_id="t_test")
    prov = lambda src: Provenance(source=src, authority="test")
    steps = [
        ("s1", "resolve_place", ResolvePlaceOut(
            provenance=prov("bbox.yaml#reference_points"),
            matched_name="Nagapattinam", lat=10.77, lon=79.84,
            in_bbox=True, match_confidence=0.8,
        )),
        ("s2", "wave_forecast", WaveForecastOut(
            provenance=prov("open_meteo_marine"),
            significant_wave_height=Range(min=0.3, max=0.56, unit=Unit.METRE),
            wave_period=Range(min=3.75, max=6.15, unit=Unit.SECOND),
            max_steepness=0.0255,
        )),
        ("s3", "wind_forecast", WindForecastOut(
            provenance=prov("open_meteo_forecast"),
            wind_speed=Range(min=8.0, max=14.5, unit=Unit.KNOT, peak=18.0),
            direction_deg=120.0,
        )),
        ("s4", "tides", TidesOut(
            provenance=prov("open_meteo_marine"),
            tidal_range=Range(min=0.1, max=0.5, unit=Unit.METRE),
            extremes=[TideExtreme(
                time=datetime(2026, 9, 7, 14, 30, tzinfo=timezone.utc),
                height_m=0.42, kind="high",
            )],
            is_limiting=False,
        )),
        ("s5", "active_alerts", ActiveAlertsOut(
            provenance=prov("gdacs"),
            count=0, alerts=[], checked_types=["cyclone"], checked=True,
        )),
    ]
    outputs, call_ids = {}, {}
    for step_id, tool, output in steps:
        record = log.record(
            tool=tool, step_id=step_id, args={},
            output=output, started_at=datetime.now(timezone.utc), duration_ms=1,
        )
        outputs[step_id] = output
        call_ids[step_id] = record.tool_call_id
    return ExecutionResult(log=log, outputs=outputs, call_ids=call_ids)


# -- routing ---------------------------------------------------------------


def test_tide_question_routes_to_conditions():
    match = keyword_intent.classify("What is the tide at Nagapattinam")
    assert match is not None and match.query_type is QueryType.CONDITIONS_REPORT


def test_weather_conditions_route_to_conditions():
    match = keyword_intent.classify("What are the weather and sea conditions near Nagapattinam")
    assert match is not None and match.query_type is QueryType.CONDITIONS_REPORT


def test_alert_status_routes_to_conditions():
    match = keyword_intent.classify("Are there any alerts in force near Cuddalore")
    assert match is not None and match.query_type is QueryType.CONDITIONS_REPORT


def test_safety_question_still_routes_to_safety():
    match = keyword_intent.classify(
        "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning"
    )
    assert match is not None and match.query_type is QueryType.SAFETY_ASSESS


def test_safest_route_hits_the_fence_not_the_report():
    # Scores both (safest 2, route 2): the tie must become the safety question
    # it is, never a vessel-free report.
    match = keyword_intent.classify("What is the safest route for my vessel from Nagapattinam")
    assert match is not None and match.query_type is QueryType.SAFETY_ASSESS


def test_safe_conditions_phrasing_hits_the_fence():
    match = keyword_intent.classify("Are conditions safe to go out tomorrow from Nagapattinam")
    assert match is not None and match.query_type is QueryType.SAFETY_ASSESS


def test_safety_phrasing_helper():
    assert keyword_intent.safety_phrasing("safest route for my vessel")
    assert keyword_intent.safety_phrasing("is it safe to venture out")
    assert not keyword_intent.safety_phrasing("tide at Nagapattinam")
    assert not keyword_intent.safety_phrasing("any alerts near Cuddalore")


# -- planner gate (LLM tier) -------------------------------------------------


def test_planner_gate_reroutes_safety_phrasing_to_safety():
    raw = fallback_plan(QueryType.CONDITIONS_REPORT, {"PLACE": "Nagapattinam"})
    output = PlannerOutput(
        intent=_intent(QueryType.CONDITIONS_REPORT).model_copy(
            update={"raw_query": "is it safest to go out from Nagapattinam"}
        ),
        state="plan",
        plan=raw,
    )
    first = LLMResult(ok=True, provider="test", model="test")
    done = _finish(
        output, "is it safest to go out from Nagapattinam", None, [], 1, "", "", first
    )
    assert done.output.intent.query_type is QueryType.SAFETY_ASSESS
    # ... and the vessel gate then fires, because no boat was named.
    assert done.state == "clarification"
    assert "vessel_class" in (done.output.clarification.missing_slots if done.output.clarification else [])


def test_planner_gate_leaves_genuine_report_alone():
    text = (
        '{"intent": {"query_type": "conditions_report", '
        '"spatial_reference": {"name": "Nagapattinam"}, "missing_slots": [], '
        '"inherited_slots": []}, "state": "chat", '
        '"chat": {"text": "x", "suggestions": []}}'
    )
    # Sanity: a genuine report parses; the gate only fires on safety phrasing.
    parsed = parse_planner_json(text, "tide at Nagapattinam")
    assert parsed.state == "chat"


# -- validator ---------------------------------------------------------------


def test_conditions_fallback_plan_validates():
    raw = fallback_plan(QueryType.CONDITIONS_REPORT, {"PLACE": "Nagapattinam"})
    result = validate_plan(raw)
    assert result.ok, result.errors


def test_conditions_fallback_needs_only_a_place():
    from agents.intent_planner_agent import _fallback_substitutions

    subs = _fallback_substitutions(_intent())
    assert subs == {"PLACE": "Nagapattinam"}


def test_conditions_plan_must_not_call_risk_score():
    raw = fallback_plan(QueryType.CONDITIONS_REPORT, {"PLACE": "Nagapattinam"})
    raw["steps"].append({
        "id": "s6", "tool": "compute_risk_score",
        "args": {"vessel_class": "frp_9m"}, "depends_on": [], "agent": "risk",
    })
    result = validate_plan(raw)
    assert not result.ok
    assert any("compute_risk_score" in e for e in result.errors)


# -- synthesis + verification --------------------------------------------------


def test_conditions_answer_verifies_with_no_verdict():
    rec = build_recommendation(_result(), _intent(), turn_id="t_test")
    assert rec.verdict is None
    assert rec.drivers == []
    assert rec.window is None
    report = verify(rec, _result().tool_call_log)
    assert report.ok, [str(e) for e in report.errors]
    assert report.numbers_checked >= 4


def test_conditions_answer_states_what_it_is_not():
    text = render(build_recommendation(_result(), _intent(), turn_id="t_test"))
    assert "Waves" in text
    assert "not a safety assessment" in text
    assert "Safe to go" not in text and "Do not go" not in text


def test_conditions_without_waves_is_an_honest_no_data():
    result = _result()
    result.outputs.pop("s2")
    rec = build_recommendation(result, _intent(), turn_id="t_test")
    assert rec.degraded
    assert "wave forecast" in rec.headline.render()
