"""Inter-agent collaboration: agents review results and extend the plan.

The property under test throughout is that **the plan differs because the data
differed**, not because the question was phrased differently. That is the line
between a multi-agent system and a pipeline with several modules in it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import agents
from agents.base import AgentRequest
from core.provenance import ToolCallLog
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from core.schemas.tool_io import GeoPoint, Provenance
from orchestrator.collaborate import (
    MAX_ADDED_STEPS,
    MAX_ROUNDS,
    _deliberate_all,
    run_with_collaboration,
)
from orchestrator.executor import ExecutionResult
from orchestrator.llm.client import LLMResult
from orchestrator.validate_plan import fallback_plan, validate_plan
from tools.geo.nearest import ResolvePlaceOut
from tools.ocean.pfz_candidates import PFZCandidate, PFZCandidatesOut
from tools.risk.risk_score import ComputeRiskScoreOut

SUBS = {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}


def _intent(query_type=QueryType.SAFETY_ASSESS):
    return Intent(
        query_type=query_type,
        raw_query="test",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )


def _result(*calls) -> ExecutionResult:
    """An ExecutionResult from (step_id, tool, output) triples."""
    log = ToolCallLog(turn_id="t_collab")
    outputs, ids = {}, {}
    for step_id, tool, output in calls:
        record = log.record(
            tool=tool,
            step_id=step_id,
            args={},
            output=output,
            started_at=datetime.now(UTC),
            duration_ms=1,
        )
        outputs[step_id] = output
        ids[step_id] = record.tool_call_id
    return ExecutionResult(log=log, outputs=outputs, call_ids=ids)


def _place():
    return ResolvePlaceOut(
        provenance=Provenance(source="bbox.yaml", authority="ORCA"),
        matched_name="Nagapattinam",
        lat=10.77,
        lon=79.84,
        in_bbox=True,
        match_confidence=0.8,
    )


def _pfz(distance_km: float):
    return PFZCandidatesOut(
        provenance=Provenance(source="erddap", authority="NOAA"),
        candidates=[
            PFZCandidate(
                centroid=GeoPoint(lat=10.9, lon=80.1),
                distance_km=distance_km,
                bearing_deg=83.0,
                score=0.6,
                rationale_codes=["front_moderate"],
            )
        ],
    )


def _risk(verdict: str):
    return ComputeRiskScoreOut(
        provenance=Provenance(source="deterministic", authority="ORCA"),
        score=0.5,
        band="x",
        verdict=verdict,
        contributions=[],
    )


def _plan_for(query_type):
    return validate_plan(fallback_plan(query_type, SUBS)).plan


# ==========================================================================
# The requests themselves
# ==========================================================================


def test_ocean_asks_geospatial_to_geofence_the_candidate_not_just_the_port():
    """The gap this closes is real.

    pfz_candidates derives zones from satellite fronts and knows nothing about
    maritime boundaries, and the plan geofences the departure point. Without
    this, ORCA could recommend a zone across the IMBL, where the penalty is
    arrest and seizure of the vessel.
    """
    result = _result(("s1", "resolve_place", _place()), ("s2", "pfz_candidates", _pfz(14.8)))
    requests = agents.agent_for("ocean").review(result, _intent(QueryType.PFZ_LOCATE))

    geofence = [r for r in requests if r.tool == "geofence_check"]
    assert len(geofence) == 1
    assert geofence[0].to_agent == "GeospatialAgent"
    assert geofence[0].critical, "a boundary crossing is not a convenience"
    assert geofence[0].args["points"] == [{"lat": 10.9, "lon": 80.1}]


def test_ocean_asks_weather_only_when_the_zone_is_far_offshore():
    """Conditions at a zone 5 km out are the conditions at the port.

    At 45 km they are not, and that is a data-driven distinction rather than a
    query-type one.
    """
    near = agents.agent_for("ocean").review(
        _result(("s1", "resolve_place", _place()), ("s2", "pfz_candidates", _pfz(5.0))),
        _intent(QueryType.PFZ_LOCATE),
    )
    far = agents.agent_for("ocean").review(
        _result(("s1", "resolve_place", _place()), ("s2", "pfz_candidates", _pfz(45.0))),
        _intent(QueryType.PFZ_LOCATE),
    )
    assert not [r for r in near if r.tool == "wave_forecast"]
    assert [r for r in far if r.tool == "wave_forecast"]


def test_no_candidates_means_no_questions():
    """An agent with nothing to report asks nothing, which is what terminates
    the loop in the common case."""
    empty = PFZCandidatesOut(provenance=Provenance(source="erddap", authority="NOAA"))
    result = _result(("s1", "resolve_place", _place()), ("s2", "pfz_candidates", empty))
    assert agents.agent_for("ocean").review(result, _intent(QueryType.PFZ_LOCATE)) == []


@pytest.mark.parametrize(
    "verdict,expected", [("go", False), ("marginal", True), ("no_go", True)]
)
def test_risk_asks_for_shelter_only_when_the_verdict_is_bad(verdict, expected):
    """The same question on two different days produces two different plans."""
    result = _result(
        ("s1", "resolve_place", _place()), ("s6", "compute_risk_score", _risk(verdict))
    )
    requests = agents.agent_for("risk").review(result, _intent())
    assert bool([r for r in requests if r.tool == "nearest_landing_centre"]) is expected


# ==========================================================================
# The loop
# ==========================================================================


def test_collaboration_extends_the_plan_and_the_new_step_runs():
    plan = _plan_for(QueryType.PFZ_LOCATE)
    before = len(plan.steps)
    outcome = run_with_collaboration(plan, _intent(QueryType.PFZ_LOCATE), turn_id="t_x")

    if not outcome.collaborated:
        pytest.skip("no candidate zone in today's data; nothing to ask about")
    assert len(outcome.plan.steps) > before
    assert outcome.rounds >= 1
    assert outcome.result.outputs


def test_a_duplicate_request_is_dropped_so_the_loop_terminates():
    """An agent that asks for the same thing every round would never stop --
    the finding that prompted it is still there next round."""
    plan = _plan_for(QueryType.PFZ_LOCATE)
    outcome = run_with_collaboration(plan, _intent(QueryType.PFZ_LOCATE), turn_id="t_x")
    signatures = [r.signature for r in outcome.requests]
    assert len(signatures) == len(set(signatures))
    assert outcome.rounds <= MAX_ROUNDS


def test_collaboration_never_adds_more_than_the_step_budget():
    plan = _plan_for(QueryType.SAFETY_ASSESS)
    outcome = run_with_collaboration(plan, _intent(), turn_id="t_x")
    assert len(outcome.plan.steps) - len(plan.steps) <= MAX_ADDED_STEPS


def test_an_agent_raising_in_review_does_not_lose_the_answer(monkeypatch):
    """One broken agent must not cost the fisherman a verdict."""

    def explode(self, result, intent):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(agents.agent_for("ocean")), "review", explode)
    outcome = run_with_collaboration(_plan_for(QueryType.SAFETY_ASSESS), _intent(), turn_id="t_x")
    assert outcome.result.outputs, "the wave-one result must survive"
    assert any("boom" in note for note in outcome.notes)


def test_a_request_goes_through_the_same_validator_as_any_other_step(monkeypatch):
    """An agent cannot smuggle in a call the validator would reject."""

    def bad(self, result, intent):
        return [
            AgentRequest(
                from_agent="OceanAgent",
                to_agent="GeospatialAgent",
                tool="resolve_place",
                args={"not_a_field": 1},
                reason="invalid on purpose",
            )
        ]

    monkeypatch.setattr(type(agents.agent_for("ocean")), "review", bad)
    outcome = run_with_collaboration(_plan_for(QueryType.SAFETY_ASSESS), _intent(), turn_id="t_x")
    assert outcome.result.outputs
    assert any("rejected" in note for note in outcome.notes)


def test_an_llm_request_for_a_safety_critical_tool_is_not_rejected_as_optional():
    """Regression for a conflation of two different meanings of "critical".

    ``AgentRequest.critical`` means "this came from the rule floor, do not drop
    it under budget pressure", and a deliberating agent may never set it.
    ``ToolSpec.safety_critical`` means "a failure must degrade the verdict
    rather than be skipped", and is a property of the tool.

    Deriving a step's ``optional`` flag from the request meant every
    LLM-proposed call to a safety-critical tool came out optional, and
    ``validate_plan()`` rejected the entire extension -- so an agent correctly
    asking for a geofence check had its request thrown out on a technicality.
    """
    from orchestrator.collaborate import _step_for
    from orchestrator.validate_plan import validate_plan
    from tools import registry

    assert registry.get("geofence_check").safety_critical

    request = AgentRequest(
        from_agent="GeospatialAgent",
        to_agent="GeospatialAgent",
        tool="geofence_check",
        args={"points": [{"lat": 10.77, "lon": 79.84}]},
        reason="proposed by a deliberating agent",
        critical=False,  # a model may never set this
    )
    step = _step_for(request, 7)
    assert step["optional"] is False, "the tool decides, not the requester"

    # And it validates when appended to a real plan. Validated in context
    # rather than alone, because a safety_assess plan must also call
    # compute_risk_score -- a separate rule, correctly enforced, that a
    # one-step fixture would trip over for the wrong reason.
    raw = {
        "intent_type": "safety_assess",
        "steps": [
            {"id": s.id, "tool": s.tool, "args": s.args, "depends_on": list(s.depends_on),
             "agent": s.agent, "optional": s.optional}
            for s in _plan_for(QueryType.SAFETY_ASSESS).steps
        ]
        + [step],
    }
    result = validate_plan(raw)
    assert result.ok, result.errors


def test_a_non_critical_tool_from_an_llm_stays_optional():
    """The other half: a model's suggestion should degrade quietly if it fails."""
    from orchestrator.collaborate import _step_for
    from tools import registry

    assert not registry.get("nearest_landing_centre").safety_critical
    step = _step_for(
        AgentRequest(
            from_agent="RiskAgent",
            to_agent="GeospatialAgent",
            tool="nearest_landing_centre",
            args={"lat": 10.77, "lon": 79.84},
            reason="suggested",
            critical=False,
        ),
        8,
    )
    assert step["optional"] is True


def test_deliberations_run_concurrently_in_agent_order(monkeypatch):
    """Deliberation latency stacks per agent unless fanned out.

    Two agents x 0.4 s sleeps finish well under the 0.8 s sequential floor,
    and results come back in agent order so traces read deterministically.
    """
    import threading
    import time

    from core.units import Range, Unit
    from tools.weather.wave_forecast import WaveForecastOut

    entered: list[str] = []

    def fake_complete(role, system, user):
        entered.append(threading.current_thread().name)
        time.sleep(0.4)
        return LLMResult(
            ok=True,
            text='{"assessment": "fine", "requests": [], "concerns": []}',
            provider="stub",
            model="stub",
        )

    monkeypatch.setattr("agents.deliberate.llm.complete", fake_complete)
    wave = WaveForecastOut(
        provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
        significant_wave_height=Range(min=0.3, max=0.56, unit=Unit.METRE),
        wave_period=Range(min=3.75, max=6.15, unit=Unit.SECOND),
    )
    result = _result(("s1", "resolve_place", _place()), ("s2", "wave_forecast", wave))
    pending = [a for a in agents.all_agents() if a.fragment(result, _intent()).step_ids]
    assert len(pending) >= 2

    started = time.monotonic()
    thoughts = _deliberate_all(pending, result, _intent())
    wall = time.monotonic() - started

    assert [t.agent for t in thoughts] == [a.name for a in pending]
    assert all(t.ok for t in thoughts)
    assert wall < 0.7, f"deliberations ran sequentially ({wall:.2f} s)"


def test_conditions_reports_skip_deliberation_but_keep_the_rule_floor(monkeypatch):
    """Read-only turns skip the LLM fan-out, never the rules.

    A conditions report carries no verdict, so deliberation can add no
    safety-critical request -- only latency (measured: the dominant slice of
    a turn). Rule-floor requests still apply, and the answer shape is intact.
    """

    def _boom(role, system, user):
        raise AssertionError("deliberation must not run on a read-only turn")

    monkeypatch.setattr("agents.deliberate.llm.complete", _boom)
    result = _result(("s1", "resolve_place", _place()))
    outcome = run_with_collaboration(
        _plan_for(QueryType.CONDITIONS_REPORT), _intent(QueryType.CONDITIONS_REPORT),
        turn_id="t_x",
    )
    assert outcome.deliberations == []
    assert outcome.result.outputs
