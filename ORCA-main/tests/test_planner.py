"""Tests for the intent + planner agent.

The LLM is stubbed throughout. What is under test is the funnel of code
around the model call -- parsing, inheritance, the governing-rule gate,
validation, replan, fallback -- because that is where the safety properties
live. A model that returns garbage must still leave the system in a state
where the question has an honest answer.
"""

from __future__ import annotations

import json

import pytest

from agents import intent_planner_agent as planner
from agents.intent_planner_agent import apply_inheritance, parse_planner_json, plan_query
from core.schemas.intent import SAFETY_CRITICAL_SLOTS, Intent, QueryType, SpatialReference, VesselClass
from orchestrator.llm.client import LLMResult
from tools import registry


def _good_plan_json(place="Nagapattinam", vessel="frp_9m", with_vessel=True) -> str:
    return json.dumps(
        {
            "intent": {
                "query_type": "safety_assess",
                "spatial_reference": {"name": place},
                "time_window": None,
                "vessel_class": vessel if with_vessel else None,
                "missing_slots": [] if with_vessel else ["vessel_class"],
                "inherited_slots": [],
            },
            "state": "plan",
            "plan": {
                "intent_type": "safety_assess",
                "steps": [
                    {"id": "s1", "tool": "resolve_place", "args": {"name": place}, "depends_on": []},
                    {"id": "s2", "tool": "wave_forecast", "args": {"lat": "$s1.lat", "lon": "$s1.lon", "hours": 24}, "depends_on": ["s1"]},
                    {"id": "s3", "tool": "wind_forecast", "args": {"lat": "$s1.lat", "lon": "$s1.lon", "hours": 24}, "depends_on": ["s1"]},
                    {"id": "s5", "tool": "active_alerts", "args": {"lat": "$s1.lat", "lon": "$s1.lon"}, "depends_on": ["s1"]},
                    {
                        "id": "s6",
                        "tool": "compute_risk_score",
                        "args": {
                            "vessel_class": vessel,
                            "wave_height": "$s2.significant_wave_height",
                            "wind_speed": "$s3.wind_speed",
                            "alerts_checked": "$s5.checked",
                            "alerts_active": "$s5.count",
                        },
                        "depends_on": ["s2", "s3", "s5"],
                    },
                ],
            },
            "clarification": None,
            "refusal": None,
        }
    )


def _stub_sequence(monkeypatch, *responses):
    """Feed the planner a sequence of canned model replies."""
    calls = iter(responses)

    def fake_complete(role, system, user):
        assert role == "planner"
        item = next(calls)
        if isinstance(item, LLMResult):
            return item
        return LLMResult(ok=True, text=item, provider="stub", model="stub-model")

    monkeypatch.setattr(planner.llm, "complete", fake_complete)


# ==========================================================================
# Parsing
# ==========================================================================


def test_parses_a_clean_response():
    out = parse_planner_json(_good_plan_json(), "safe?")
    assert out.state == "plan"
    assert out.intent.raw_query == "safe?"


def test_tolerates_code_fences_and_leading_prose():
    text = "Here is the plan:\n```json\n" + _good_plan_json() + "\n```"
    assert parse_planner_json(text, "q").state == "plan"


def test_raw_query_is_ours_not_the_models_paraphrase():
    data = json.loads(_good_plan_json())
    data["intent"]["raw_query"] = "the model rewrote this"
    out = parse_planner_json(json.dumps(data), "what the user actually typed")
    assert out.intent.raw_query == "what the user actually typed"


def test_two_populated_blocks_are_rejected():
    data = json.loads(_good_plan_json())
    data["refusal"] = {"reason": "out_of_scope", "explanation_template": "no"}
    with pytest.raises(Exception, match="exactly one state"):
        parse_planner_json(json.dumps(data), "q")


# ==========================================================================
# The governing-rule gate
# ==========================================================================


def test_gate_slots_match_the_schema_constant():
    """Two places name the safety-critical slots. They must agree."""
    for key in planner._CLARIFICATION_QUESTIONS:
        assert set(key) <= set(SAFETY_CRITICAL_SLOTS)


def test_a_safety_plan_missing_the_vessel_class_is_forced_to_clarification(monkeypatch):
    """The model returned a plan anyway. Code overrules it.

    The prompt tells the model to ask. This gate is what happens when it does
    not listen, and it is the reason the rule is a rule rather than a hope.
    """
    _stub_sequence(monkeypatch, _good_plan_json(with_vessel=False))
    result = plan_query("is it safe from Nagapattinam tomorrow?")
    assert result.state == "clarification"
    assert "vessel_class" in result.output.clarification.missing_slots
    assert result.output.clarification.options == [v.value for v in VesselClass]
    assert any("forced clarification" in n for n in result.notes)


def test_a_non_safety_query_does_not_need_a_vessel_class(monkeypatch):
    text = json.dumps(
        {
            "intent": {"query_type": "geofence_check", "spatial_reference": {"name": "Rameswaram"}, "vessel_class": None, "missing_slots": [], "inherited_slots": []},
            "state": "plan",
            "plan": {
                "intent_type": "geofence_check",
                "steps": [
                    {"id": "s1", "tool": "resolve_place", "args": {"name": "Rameswaram"}, "depends_on": []},
                    {"id": "s2", "tool": "geofence_check", "args": {"points": [{"lat": "$s1.lat", "lon": "$s1.lon"}], "buffer_km": 5.0}, "depends_on": ["s1"]},
                ],
            },
        }
    )
    _stub_sequence(monkeypatch, text)
    result = plan_query("which areas must I avoid near Rameswaram?")
    assert result.state == "plan"
    assert result.plan is not None


# ==========================================================================
# Inheritance
# ==========================================================================


def test_missing_slots_are_inherited_from_context_and_recorded():
    previous = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="earlier",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    now = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="and tomorrow afternoon?", missing_slots=["spatial_reference", "vessel_class"])
    merged = apply_inheritance(now, previous)
    assert merged.spatial_reference.name == "Nagapattinam"
    assert merged.vessel_class is VesselClass.FRP_9M
    assert set(merged.inherited_slots) == {"spatial_reference", "vessel_class"}
    assert merged.missing_slots == []
    assert merged.blocking_gaps() == []


def test_a_follow_up_plans_instead_of_asking_when_context_fills_the_gaps(monkeypatch):
    previous = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="earlier",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    _stub_sequence(monkeypatch, _good_plan_json(with_vessel=False))
    result = plan_query("what about tomorrow afternoon?", context=previous)
    assert result.state == "plan"
    assert "vessel_class" in result.output.intent.inherited_slots


def test_explicit_slots_are_not_overwritten_by_context():
    previous = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="x", vessel_class=VesselClass.MECHANISED_TRAWLER)
    now = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query="y", vessel_class=VesselClass.KATTUMARAM)
    assert apply_inheritance(now, previous).vessel_class is VesselClass.KATTUMARAM


# ==========================================================================
# Validation, replan, fallback
# ==========================================================================


def test_an_invalid_plan_gets_one_replan_with_the_errors_fed_back(monkeypatch):
    bad = json.loads(_good_plan_json())
    bad["plan"]["steps"][1]["tool"] = "summon_kraken"
    seen: list[str] = []

    def fake_complete(role, system, user):
        seen.append(user)
        return LLMResult(ok=True, text=json.dumps(bad) if len(seen) == 1 else _good_plan_json(), provider="stub", model="m")

    monkeypatch.setattr(planner.llm, "complete", fake_complete)
    result = plan_query("safe from Nagapattinam in my FRP boat?")
    assert result.state == "plan"
    assert result.attempts == 2
    assert not result.used_fallback
    assert "summon_kraken" in seen[1], "the replan prompt must carry the validation error"


def test_two_bad_plans_fall_back_to_the_hardcoded_plan(monkeypatch):
    bad = json.loads(_good_plan_json())
    bad["plan"]["steps"][1]["tool"] = "summon_kraken"
    _stub_sequence(monkeypatch, json.dumps(bad), json.dumps(bad))
    result = plan_query("safe from Nagapattinam in my FRP boat?")
    assert result.state == "plan"
    assert result.used_fallback
    assert result.plan is not None and result.plan.is_fallback


def test_llm_unavailable_with_context_uses_the_fallback_plan(monkeypatch):
    """The venue loses internet. A follow-up in an existing conversation still answers."""
    previous = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="earlier",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    _stub_sequence(monkeypatch, LLMResult(ok=False, error="connection refused"))
    result = plan_query("and tomorrow?", context=previous)
    assert result.state == "plan"
    assert result.used_fallback
    assert result.llm_provider == "none"


def test_llm_unavailable_still_asks_for_safety_critical_slots(monkeypatch):
    """No model, no place, no boat -> ask. The keyword tier changed how the
    query type is found, not whether missing slots are asked for.

    Since 2026-09-06 a keyword classifier (agents/keyword_intent.py) recognises
    the query type when no provider answers, so the user is asked about the
    right thing instead of being asked generically. It proposes a *type* only;
    the safety slot gate is untouched.
    """
    _stub_sequence(monkeypatch, LLMResult(ok=False, error="connection refused"))
    result = plan_query("is it safe to go out?")
    assert result.state == "clarification"
    assert result.output.intent.query_type is QueryType.SAFETY_ASSESS
    assert any("keyword" in n for n in result.notes)


def test_keyword_tier_never_invents_a_place_or_a_boat(monkeypatch):
    """The tier below Ollama may guess the question, never the safety slots."""
    _stub_sequence(monkeypatch, LLMResult(ok=False, error="connection refused"))
    result = plan_query("is it safe to go out tomorrow?")
    intent = result.output.intent
    assert intent.spatial_reference is None
    assert intent.vessel_class is None


def test_an_unclassifiable_question_still_falls_through_to_asking(monkeypatch):
    """A vague request with no model and no context becomes a question.

    The example used to be "hello there", which Gate 0 now catches earlier as a
    greeting -- a more specific answer to the same situation. This one is not a
    pleasantry, so it reaches the keyword tier and finds nothing there either.
    """
    _stub_sequence(monkeypatch, LLMResult(ok=False, error="connection refused"))
    result = plan_query("can you tell me something about that")
    assert result.state == "clarification"
    assert any("inconclusive" in n for n in result.notes)


def test_unparseable_response_is_retried_once_then_falls_back(monkeypatch):
    previous = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="earlier",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    _stub_sequence(monkeypatch, "I'd be happy to help! Here's my thinking...", "still not json")
    result = plan_query("and tomorrow?", context=previous)
    assert result.attempts == 2
    assert result.state == "plan"
    assert result.used_fallback


def test_a_query_type_whose_tools_are_not_built_refuses_honestly(monkeypatch):
    """A plan naming a tool we cannot run is rejected, and so is the fallback.

    The result is a refusal saying the data is not connected -- not a crash,
    not a fake answer.

    **The rule is permanent; the example is not.** This test used to rely on
    pfz_locate being genuinely unbuildable, because the ocean tools were
    blocked on a Copernicus account. They were wired to NOAA CoastWatch on
    2026-09-06 and now run, so the test quietly began asserting the opposite
    of the truth. It now *makes* a tool unimplemented for the duration instead
    of borrowing whichever one happens to be unfinished, which is what stops
    it rotting again the next time someone finishes something.
    """
    from dataclasses import replace

    spec = registry.get("pfz_candidates")
    monkeypatch.setitem(registry._REGISTRY, "pfz_candidates", replace(spec, fn=None))
    assert not registry.get("pfz_candidates").implemented

    text = json.dumps(
        {
            "intent": {"query_type": "pfz_locate", "spatial_reference": {"name": "Nagapattinam"}, "vessel_class": None, "missing_slots": [], "inherited_slots": []},
            "state": "plan",
            "plan": {
                "intent_type": "pfz_locate",
                "steps": [
                    {"id": "s1", "tool": "resolve_place", "args": {"name": "Nagapattinam"}, "depends_on": []},
                    {"id": "s2", "tool": "pfz_candidates", "args": {"lat": "$s1.lat", "lon": "$s1.lon"}, "depends_on": ["s1"]},
                ],
            },
        }
    )
    _stub_sequence(monkeypatch, text, text)
    result = plan_query("where are the fish near Nagapattinam?")
    assert result.state == "refusal"
    assert result.output.refusal.reason.value == "no_data"


def test_pfz_locate_now_plans_because_the_ocean_tools_are_live(monkeypatch):
    """The other half of the change above, pinned so a regression is visible.

    pfz_candidates, thermal_front and chl_anomaly run against NOAA CoastWatch
    MUR SST and gap-filled VIIRS chlorophyll. A pfz_locate question is
    therefore answerable, and must no longer be refused as no_data.
    """
    assert registry.get("pfz_candidates").implemented
    text = json.dumps(
        {
            "intent": {"query_type": "pfz_locate", "spatial_reference": {"name": "Nagapattinam"}, "vessel_class": None, "missing_slots": [], "inherited_slots": []},
            "state": "plan",
            "plan": {
                "intent_type": "pfz_locate",
                "steps": [
                    {"id": "s1", "tool": "resolve_place", "args": {"name": "Nagapattinam"}, "depends_on": []},
                    {"id": "s2", "tool": "pfz_candidates", "args": {"lat": "$s1.lat", "lon": "$s1.lon"}, "depends_on": ["s1"]},
                ],
            },
        }
    )
    _stub_sequence(monkeypatch, text)
    result = plan_query("where are the fish near Nagapattinam?")
    assert result.state == "plan"
    assert [step.tool for step in result.plan.steps] == ["resolve_place", "pfz_candidates"]

def test_the_model_may_not_declare_a_real_query_type_out_of_scope(monkeypatch):
    """Regression for a live finding.

    Asked where the fish were, the model saw no pfz tool in the catalogue and
    refused with out_of_scope -- and wrote that this system does not locate
    fishing zones. It does; the data is not connected yet. The model's
    classification (pfz_locate) is kept, the refusal is kept, but the reason
    becomes no_data with our own explanation, because the four query types
    are always in scope and the model does not get to say otherwise.
    """
    text = json.dumps(
        {
            "intent": {"query_type": "pfz_locate", "spatial_reference": {"name": "Nagapattinam"}, "vessel_class": None, "missing_slots": [], "inherited_slots": []},
            "state": "refusal",
            "plan": None,
            "clarification": None,
            "refusal": {"reason": "out_of_scope", "explanation_template": "I cannot locate fishing zones."},
        }
    )
    _stub_sequence(monkeypatch, text)
    result = plan_query("where are the fish near Nagapattinam?")
    assert result.state == "refusal"
    assert result.output.refusal.reason.value == "no_data"
    assert "cannot locate" not in result.output.refusal.explanation_template
    assert "not connected" in result.output.refusal.explanation_template
    assert any("overridden" in n for n in result.notes)


def test_a_genuinely_out_of_scope_refusal_is_left_alone(monkeypatch):
    """The override is narrow: only the four real query types are protected."""
    text = json.dumps(
        {
            "intent": {"query_type": "safety_assess", "spatial_reference": None, "vessel_class": None, "missing_slots": [], "inherited_slots": []},
            "state": "refusal",
            "refusal": {"reason": "out_of_scope", "explanation_template": "I do not give stock tips."},
        }
    )
    _stub_sequence(monkeypatch, text)
    # The model mislabelled a stock-tip question as safety_assess with no
    # slots; the safety gate fires first and asks for slots. That is the
    # correct outcome for a misclassification: ask, do not guess.
    result = plan_query("should I buy shares in a trawler company?")
    assert result.state == "clarification"


def test_the_prompt_is_a_markdown_file_with_the_tool_block_injected():
    """CLAUDE.md: never hand-maintain a tool list in a prompt file."""
    raw = planner.PROMPT_PATH.read_text(encoding="utf-8")
    assert "{TOOLS}" in raw, "the template must carry the placeholder, not a tool list"
    rendered = planner._system_prompt()
    assert "{TOOLS}" not in rendered
    assert "**compute_risk_score**" in rendered
    # Derived from the registry rather than naming a tool, so that finishing
    # a tool cannot turn this assertion false. It broke exactly that way when
    # pfz_candidates was implemented on 2026-09-06.
    for spec in registry.all_specs():
        if not spec.implemented:
            assert f"**{spec.name}**" not in rendered, (
                f"{spec.name} has no implementation and must not be offered to the planner"
            )


def test_a_slot_the_model_copied_from_context_is_still_recorded_as_inherited():
    """Found on the first live multi-turn session, 2026-09-06.

    apply_inheritance only filled slots the model left None. But the model sees
    the previous turn in its prompt and copies the slots itself, so nothing was
    left to fill and nothing was recorded -- and the answer to "what about the
    day after?" silently described a boat and a port the user never mentioned
    in that turn. CLAUDE.md: inheritance is recorded, never silent.
    """
    context = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe from Nagapattinam in my FRP boat?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    # The model filled both slots itself; this turn's words support neither.
    copied = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="what about the day after?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    out = apply_inheritance(copied, context)
    assert set(out.inherited_slots) == {"spatial_reference", "vessel_class"}


def test_a_slot_the_user_restated_is_not_marked_inherited():
    """The other side of it: restating a place is not an assumption."""
    context = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="earlier",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    restated = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe from Nagapattinam tomorrow in my FRP boat?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    assert apply_inheritance(restated, context).inherited_slots == []


# ==========================================================================
# Gate 0: a greeting is not a follow-up
# ==========================================================================


def test_a_greeting_does_not_inherit_the_previous_question(monkeypatch):
    """Found live on 2026-09-06.

    After a fishing-zone question, typing "hello" returned a full fishing-zone
    answer -- distance, bearing, boundary check and all. Context inheritance was
    working as designed and the model, shown a previous turn and nothing else,
    continued it. Inheritance fills a *missing slot*; it may not manufacture a
    request the user never made.
    """
    previous = Intent(
        query_type=QueryType.PFZ_LOCATE,
        raw_query="where are the fish near Rameswaram?",
        spatial_reference=SpatialReference(name="Rameswaram"),
        vessel_class=VesselClass.MECHANISED_TRAWLER,
    )
    # No stub: the gate must fire before the model is ever called.
    result = plan_query("hello", context=previous)

    assert result.state == "clarification"
    assert result.plan is None
    assert any("greeting" in note for note in result.notes)


@pytest.mark.parametrize("greeting", ["hello", "Hi!", "good morning", "thanks", "ok"])
def test_pleasantries_are_all_caught(greeting):
    assert plan_query(greeting).state == "clarification"


@pytest.mark.parametrize(
    "query",
    [
        "what about the day after?",
        "and where are the fish?",
        "hello, is it safe tomorrow?",
    ],
)
def test_a_real_follow_up_is_not_mistaken_for_a_greeting(query):
    """The guard has to be narrow.

    "What about the day after?" carries no marine keyword either, so a rule
    like "must mention the sea" would break multi-turn conversation, which is a
    stated requirement of the problem statement.
    """
    from agents.keyword_intent import is_conversational_filler

    assert not is_conversational_filler(query)


def test_the_narrator_prompt_forbids_inventing_a_verdict():
    """Only a safety answer has a verdict.

    A fishing-zone answer that opened "do not go out until you check the safety
    forecast" was shown to a user on 2026-09-06. It reads as a refusal to sail;
    nothing had refused anything. The caveat had been promoted to the lead
    because the prompt only described safety answers.
    """
    from agents import narrate

    prompt = narrate.PROMPT_PATH.read_text(encoding="utf-8").lower()
    assert "do not create one" in prompt
    assert "caveat is never the opening sentence" in prompt
