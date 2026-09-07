"""Domain agents: ownership, plan composition, and fragment policy.

These tests are deliberately offline. Every one of them either uses the
registry alone or a hand-built ExecutionResult, so the suite still passes on a
machine with no cache and no network -- the same property that makes the
deterministic core worth having.

The live path is exercised by scripts/try_agents.py, which needs a populated
cache and is not a test.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import agents
from agents.base import DomainFragment, all_agents, compose_plan, fragments_for
from core.provenance import ToolCallLog
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from core.schemas.tool_io import Provenance, ToolStatus
from core.units import Range, Unit
from orchestrator.executor import ExecutionResult
from orchestrator.validate_plan import validate_plan
from tools import registry
from tools.weather.alerts import ActiveAlertsOut
from tools.weather.wave_forecast import WaveForecastOut


def _intent(query_type=QueryType.SAFETY_ASSESS, place="Nagapattinam", vessel=VesselClass.FRP_9M):
    return Intent(
        query_type=query_type,
        raw_query="test",
        spatial_reference=SpatialReference(name=place) if place else None,
        vessel_class=vessel,
    )


def _substitute(node, values):
    if isinstance(node, str) and node.startswith("$") and node[1:] in values:
        return values[node[1:]]
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def _composed_and_substituted(intent):
    raw = compose_plan(intent)
    assert raw is not None
    values = {"PLACE": intent.spatial_reference.name}
    if intent.vessel_class:
        values["VESSEL_CLASS"] = intent.vessel_class.value
    return _substitute(raw, values)


# ==========================================================================
# Ownership
# ==========================================================================


def test_every_registered_agent_group_has_an_agent():
    """A tool whose group has no agent would never be planned by composition."""
    grouped = {spec.agent for spec in registry.all_specs()}
    grouped.discard(registry.AgentGroup.CATALOGUE)  # deterministic lookup, no agent
    covered = {agent.group for agent in all_agents()}
    assert grouped <= covered, f"tool groups with no agent: {grouped - covered}"


def test_agents_own_their_tools_via_the_registry_not_a_hardcoded_list():
    """The same rule as the planner's tool block: no hand-maintained lists."""
    for agent in all_agents():
        for tool_name in agent.tools():
            assert registry.get(tool_name).agent == agent.group


def test_no_tool_is_owned_by_two_agents():
    seen: dict[str, str] = {}
    for agent in all_agents():
        for tool_name in agent.tools():
            assert tool_name not in seen, f"{tool_name} claimed by {seen[tool_name]} and {agent.group}"
            seen[tool_name] = agent.group


def test_every_domain_agent_has_a_deliberation_prompt():
    """Each domain agent reasons about its own results, so each needs a prompt.

    **This test used to assert the opposite.** The four prompt files were kept
    deliberately empty, on the reasoning that a domain agent retrieves and
    computes and therefore belongs in code. That reasoning still holds for
    everything the agent *reports* -- every number still comes from a tool --
    but it was applied too widely: deciding *what else to look at* is a
    judgement, not a computation, and hardcoding those decisions is what made
    collaboration a lookup table with two entries.

    The line that replaced it is the one below: an agent may reason about what
    to do, and may not produce a number.
    """
    from agents.deliberate import _prompt_for

    for agent in all_agents():
        prompt = _prompt_for(agent)
        assert prompt, f"{agent.name} has no deliberation prompt"
        assert "{TOOLS}" not in prompt, "the tool catalogue must be injected"


def test_every_deliberation_prompt_forbids_numbers():
    """The governing rule, enforced at the prompt and again in code.

    A domain agent's words reach the user as caveats, which is exactly where an
    invented figure would look most authoritative. The prompt says not to;
    strip_numbers() makes sure.
    """
    from agents.deliberate import _prompt_for, strip_numbers

    for agent in all_agents():
        prompt = _prompt_for(agent)
        assert "never write a number" in prompt.lower(), agent.name

    assert strip_numbers("waves may reach 3.5 m") == "waves may reach m"
    assert strip_numbers("the zone is 55 km out") == "the zone is km out"
    assert strip_numbers("check the route home") == "check the route home"


def test_a_deliberating_agent_cannot_mark_its_own_request_critical():
    """Safety-critical requests come from the rule floor, never from a model.

    Forgetting to geofence a candidate zone means a fisherman arrested in Sri
    Lankan waters. That check is not left to a model's judgement on the day, and
    a model cannot promote its own suggestion to the same standing.
    """
    import json
    from unittest.mock import patch

    from agents.deliberate import deliberate
    from orchestrator.llm.client import LLMResult

    reply = json.dumps({
        "assessment": "a",
        "requests": [{
            "to_agent": "GeospatialAgent", "tool": "geofence_check",
            "args": {"points": [{"lat": 10.0, "lon": 80.0}]},
            "reason": "mine", "critical": True,
        }],
        "concerns": [],
    })
    with patch(
        "agents.deliberate.llm.complete",
        return_value=LLMResult(ok=True, text=reply, provider="stub", model="stub"),
    ):
        thought = deliberate(agents.agent_for("ocean"), _result_with("s2", "pfz_candidates", None), _intent())

    assert thought.ok
    assert thought.requests
    assert all(not r.critical for r in thought.requests)


def test_an_invented_tool_name_is_dropped_before_it_reaches_the_validator():
    """Caught here so the trace says the model invented a tool, rather than
    surfacing later as a bare validation error."""
    import json
    from unittest.mock import patch

    from agents.deliberate import deliberate
    from orchestrator.llm.client import LLMResult

    reply = json.dumps({
        "assessment": "a",
        "requests": [{"to_agent": "WeatherAgent", "tool": "predict_the_future", "args": {}, "reason": "x"}],
        "concerns": [],
    })
    with patch(
        "agents.deliberate.llm.complete",
        return_value=LLMResult(ok=True, text=reply, provider="stub", model="stub"),
    ):
        thought = deliberate(agents.agent_for("ocean"), _result_with("s2", "pfz_candidates", None), _intent())

    assert thought.ok
    assert thought.requests == []


# ==========================================================================
# Composition
# ==========================================================================


@pytest.mark.parametrize(
    "query_type",
    [QueryType.SAFETY_ASSESS, QueryType.PFZ_LOCATE, QueryType.GEOFENCE_CHECK, QueryType.CAUSAL_EXPLAIN],
)
def test_composed_plans_validate_for_every_query_type(query_type):
    """All four query types compose into a runnable plan.

    Before the ocean tools were wired on 2026-09-06 this was true of two of
    them. It is the headline of that change and worth pinning.
    """
    plan = _composed_and_substituted(_intent(query_type))
    result = validate_plan(plan)
    assert result.ok, f"{query_type.value}: {result.errors}"


def test_geospatial_always_holds_s1_because_it_owns_the_coordinate():
    for query_type in QueryType:
        intent = _intent(query_type)
        raw = compose_plan(intent)
        if raw is None:
            continue
        first = raw["steps"][0]
        assert first["id"] == "s1"
        assert first["tool"] == "resolve_place"


def test_composition_returns_none_without_a_place():
    """No coordinate, no plan. The caller must ask rather than guess."""
    assert compose_plan(_intent(place=None)) is None


def test_risk_declines_to_plan_without_a_vessel_class():
    """Scoring a boat we have no limits for is worse than refusing to score it."""
    intent = _intent(vessel=None)
    raw = compose_plan(intent)
    assert raw is not None  # geospatial and weather still contribute
    assert "compute_risk_score" not in [s["tool"] for s in raw["steps"]]


def test_the_alert_check_is_never_optional_in_a_composed_plan():
    """validate_plan enforces this too; here it is asserted at the source."""
    plan = _composed_and_substituted(_intent())
    alert_steps = [s for s in plan["steps"] if s["tool"] == "active_alerts"]
    assert alert_steps
    assert all(s["optional"] is False for s in alert_steps)


def test_symbolic_step_references_resolve_to_real_ids():
    """The risk agent cannot know the weather agent's numbering, so it aliases."""
    plan = _composed_and_substituted(_intent())
    risk = next(s for s in plan["steps"] if s["tool"] == "compute_risk_score")
    wave = next(s for s in plan["steps"] if s["tool"] == "wave_forecast")
    assert risk["args"]["wave_height"] == f"${wave['id']}.significant_wave_height"
    assert wave["id"] in risk["depends_on"]
    assert not any("$s" in dep for dep in risk["depends_on"]), "aliases must be resolved"


def test_pfz_passes_vessel_class_so_range_is_capped():
    """A 60 km zone is not a recommendation for a boat that works 12 km out."""
    plan = _composed_and_substituted(_intent(QueryType.PFZ_LOCATE, vessel=VesselClass.KATTUMARAM))
    pfz = next(s for s in plan["steps"] if s["tool"] == "pfz_candidates")
    assert pfz["args"]["vessel_class"] == "kattumaram"


# ==========================================================================
# Fragment policy -- the judgement each domain owns
# ==========================================================================


def _result_with(step_id: str, tool: str, output) -> ExecutionResult:
    """A minimal ExecutionResult carrying one recorded call."""
    log = ToolCallLog(turn_id="t_test")
    record = log.record(
        tool=tool,
        step_id=step_id,
        args={},
        output=output,
        started_at=datetime.now(timezone.utc),
        duration_ms=1,
    )
    return ExecutionResult(
        log=log,
        outputs={step_id: output},
        call_ids={step_id: record.tool_call_id},
    )


def test_a_failed_alert_check_is_safety_critical_and_blocks_a_verdict():
    """Not knowing whether a cyclone is active is not knowing there is none."""
    failed = ActiveAlertsOut(
        provenance=Provenance(source="imd_incois_alerts", authority="IMD/INCOIS"),
        status=ToolStatus.FAILED,
        error="no source wired",
        count=0,
        checked=False,
    )
    result = _result_with("s5", "active_alerts", failed)
    fragment = agents.agent_for("weather").fragment(result, _intent())

    assert fragment.status is ToolStatus.FAILED
    assert fragment.blocks_verdict
    assert any(f.key == "active_alerts_failed" and f.safety_critical for f in fragment.findings)


def test_a_failed_ocean_tool_does_not_block_a_verdict():
    """The mirror image, and the reason each domain owns its own policy.

    A missing chlorophyll layer costs the answer its fishing advice. It does
    not put anyone to sea in conditions nobody checked.
    """
    from tools.ocean.chl_anomaly import ChlAnomalyOut

    failed = ChlAnomalyOut(
        provenance=Provenance(source="viirs", authority="NOAA"),
        status=ToolStatus.FAILED,
        error="no cached chlorophyll",
        concentration=Range(min=0.0, max=0.0, unit=Unit.MILLIGRAM_PER_CUBIC_METRE),
    )
    result = _result_with("s2", "chl_anomaly", failed)
    fragment = agents.agent_for("ocean").fragment(result, _intent(QueryType.CAUSAL_EXPLAIN))

    assert fragment.status is ToolStatus.FAILED
    assert not fragment.blocks_verdict
    assert all(not f.safety_critical for f in fragment.findings)


def test_a_healthy_wave_read_becomes_slots_not_prose():
    """CLAUDE.md: claims are slot templates, never finished sentences."""
    output = WaveForecastOut(
        provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
        significant_wave_height=Range(min=0.3, max=0.56, unit=Unit.METRE),
        wave_period=Range(min=3.75, max=6.15, unit=Unit.SECOND),
        max_steepness=0.0255,
    )
    result = _result_with("s2", "wave_forecast", output)
    fragment = agents.agent_for("weather").fragment(result, _intent())

    finding = next(f for f in fragment.findings if f.key == "wave_height")
    assert finding.slots["min_m"] == 0.3
    assert finding.slots["max_m"] == 0.56
    assert finding.tool_call_id, "a finding must cite the call it rests on"
    # No sentence anywhere in the payload.
    assert " the " not in str(finding.slots).lower()


def test_fragments_skip_domains_with_no_steps():
    """A geofence answer must not carry a hollow ocean block."""
    output = WaveForecastOut(
        provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
        significant_wave_height=Range(min=0.3, max=0.5, unit=Unit.METRE),
        wave_period=Range(min=4.0, max=6.0, unit=Unit.SECOND),
    )
    result = _result_with("s2", "wave_forecast", output)
    produced = {f.agent for f in fragments_for(result, _intent())}
    assert produced == {"WeatherAgent"}


def test_every_finding_carries_a_tool_call_id_the_verifier_could_walk():
    """The evidence link is what makes the audit trail real, not decorative."""
    output = WaveForecastOut(
        provenance=Provenance(source="open_meteo_marine", authority="Open-Meteo"),
        significant_wave_height=Range(min=1.0, max=2.0, unit=Unit.METRE),
        wave_period=Range(min=4.0, max=6.0, unit=Unit.SECOND),
    )
    result = _result_with("s2", "wave_forecast", output)
    fragment = agents.agent_for("weather").fragment(result, _intent())
    log = result.tool_call_log
    for finding in fragment.findings:
        assert finding.tool_call_id in log


def test_a_fragment_has_no_verdict_of_its_own():
    """Assembling a verdict is synthesis's job. Four agents with four opinions
    about how safe the day is would be four chances to disagree."""
    assert "verdict" not in DomainFragment.model_fields
    assert "headline" not in DomainFragment.model_fields
    assert "confidence" not in DomainFragment.model_fields


def test_only_an_answered_turn_can_supply_context_to_a_follow_up():
    """Regression for a silent feature outage found on 2026-09-06.

    Turn.inheritable compared state against "plan", which is the *planner's*
    vocabulary; run_turn records "answer". The condition was never true, so
    multi-turn context never worked -- and it failed as a missing feature
    rather than as an error, which is why nothing caught it until a live
    two-question session was tried.
    """
    from orchestrator.session import SessionStore, Turn

    store = SessionStore()
    intent = _intent()
    # "error" was added on 2026-09-07. A turn that resolved a place and a
    # vessel and then failed in synthesis still established those slots -- the
    # user said them, and making them repeat themselves because our assembly
    # step raised charges our failure to them.
    for state, expected in (
        ("answer", True),
        ("error", True),
        ("clarification", False),
        ("refusal", False),
        ("plan", False),  # the planner's word, deliberately not accepted here
    ):
        store.forget("s1")
        store.record("s1", Turn(turn_id="t1", query="q", intent=intent, state=state))
        assert (store.context_for("s1") is not None) is expected, state


def test_a_reply_to_a_clarification_fills_the_slot_it_answered():
    """Found in the UI on 2026-09-06: an infinite question loop.

    Asked "which landing centre and what boat", the user clicked
    MECHANISED TRAWLER -- and was asked the identical question again, forever.
    SessionStore.context_for deliberately refuses clarifications, on the
    reasoning that the question we asked carries no slots. That is true of the
    question and false of the reply to it, which had nowhere to go.
    """
    from orchestrator.session import SessionStore, Turn
    from orchestrator.turn import _answer_to_clarification

    store = SessionStore()
    asked = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="tell me safest route")
    store.record(
        "s1",
        Turn(
            turn_id="t1",
            query="tell me safest route",
            intent=asked,
            state="clarification",
            missing_slots=["spatial_reference", "vessel_class"],
        ),
    )

    pending = store.pending_question("s1")
    assert pending is not None, "a clarification must be retrievable as a pending question"

    merged = _answer_to_clarification("mechanised_trawler", pending)
    assert merged is not None
    assert merged.vessel_class is VesselClass.MECHANISED_TRAWLER
    assert merged.spatial_reference is None, "only the slot that was answered"

    # And the second reply completes it rather than starting over.
    store.record(
        "s1",
        Turn(
            turn_id="t2",
            query="mechanised_trawler",
            intent=merged,
            state="clarification",
            missing_slots=["spatial_reference"],
        ),
    )
    done = _answer_to_clarification("Nagapattinam", store.pending_question("s1"))
    assert done.spatial_reference.name == "Nagapattinam"
    assert done.vessel_class is VesselClass.MECHANISED_TRAWLER
    assert done.blocking_gaps() == []


def test_a_reply_that_answers_nothing_is_planned_as_a_fresh_question():
    """The user changed the subject. Forcing the reply into the old question
    would answer something nobody asked."""
    from orchestrator.session import Turn
    from orchestrator.turn import _answer_to_clarification

    pending = Turn(
        turn_id="t1",
        query="safe?",
        intent=Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="safe?"),
        state="clarification",
        missing_slots=["spatial_reference", "vessel_class"],
    )
    assert _answer_to_clarification("why has my catch declined?", pending) is None


def test_a_stale_clarification_is_not_treated_as_pending():
    """A reply an hour later is a new conversation, not a slot value."""
    from datetime import datetime, timedelta, timezone

    from orchestrator.session import PENDING_TTL_MINUTES, SessionStore, Turn

    store = SessionStore()
    store.record(
        "s1",
        Turn(
            turn_id="t1",
            query="safe?",
            intent=Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="safe?"),
            state="clarification",
            missing_slots=["vessel_class"],
            created_at=datetime.now(timezone.utc)
            - timedelta(minutes=PENDING_TTL_MINUTES + 5),
        ),
    )
    assert store.pending_question("s1") is None


def test_a_numeric_only_concern_cannot_destroy_an_answer():
    """The number guard must not be able to break the thing it protects.

    strip_numbers("3.5") is the empty string, Caveat requires min_length=1, and
    building one raised -- taking the whole recommendation with it and
    returning "the answer could not be assembled". Intermittent, because it
    depended on what the model happened to write that turn.
    """
    import json
    from unittest.mock import patch

    from agents.deliberate import deliberate
    from core.provenance import ToolCallLog
    from core.schemas.recommendation import Caveat
    from orchestrator.executor import ExecutionResult
    from orchestrator.llm.client import LLMResult

    reply = json.dumps(
        {
            "assessment": "2.5",
            "requests": [],
            "concerns": ["3.5", "28", "the zone lies far offshore for this boat"],
        }
    )
    with patch(
        "agents.deliberate.llm.complete",
        return_value=LLMResult(ok=True, text=reply, provider="stub", model="stub"),
    ):
        thought = deliberate(
            agents.agent_for("ocean"),
            ExecutionResult(log=ToolCallLog("t")),
            _intent(QueryType.PFZ_LOCATE),
        )

    assert all(c.strip() for c in thought.concerns), "no empty concern may survive"
    assert len(thought.concerns) == 1
    for concern in thought.concerns:
        Caveat(text=concern)  # must not raise
