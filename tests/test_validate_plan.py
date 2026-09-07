"""Tests for the registry and deterministic plan validation.

Plan validation is the gate between the LLM and anything that executes. These
tests are mostly about what it *refuses*, since accepting a good plan is the
easy half.
"""

from __future__ import annotations

import pytest

from core.schemas.intent import QueryType
from orchestrator.validate_plan import FALLBACK_PLANS, fallback_plan, validate_plan
from tools import registry


# ==========================================================================
# Registry
# ==========================================================================


def test_all_four_agent_groups_have_tools():
    agents = {s.agent for s in registry.all_specs()}
    assert {"ocean", "weather", "geospatial", "risk"} <= agents


def test_safety_critical_tools_are_marked():
    critical = {s.name for s in registry.all_specs() if s.safety_critical}
    # Each of these can change a verdict, so each must be non-optional in a plan.
    assert {"wave_forecast", "wind_forecast", "active_alerts", "compute_risk_score"} <= critical


def test_registering_a_duplicate_name_is_an_error():
    """Silent overwrite would make the winner depend on import order."""
    spec = registry.get("tides")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            "tides",
            description="dupe",
            input_model=spec.input_model,
            output_model=spec.output_model,
            agent="weather",
        )


def test_unknown_tool_lookup_names_what_is_available():
    with pytest.raises(KeyError, match="Registered:"):
        registry.get("summon_kraken")


def test_planner_block_is_generated_from_the_schemas():
    """The prompt cannot drift from the code, because it is derived from it."""
    block = registry.planner_tool_block(include_unimplemented=True)
    assert "**wave_forecast**" in block
    assert "significant_wave_height" not in block  # outputs are named, not expanded
    assert "lat (float, required)" in block
    assert "[SAFETY-CRITICAL]" in block


def test_planner_block_renders_optional_types_readably():
    """'Union' tells a planner nothing about what to put in the argument."""
    block = registry.planner_tool_block(include_unimplemented=True)
    assert "Union" not in block
    assert "float or null" in block
    assert "list[GeoPoint]" in block
    assert "tuple[float, float, float, float]" in block


def test_unimplemented_tools_are_not_advertised():
    """The planner must not be able to plan a call we cannot run.

    Asserts the property rather than a snapshot of how much is built: every
    name in the default block is implemented, and every unimplemented one is
    absent. That stays true as Phase 1 fills tools in, whereas an
    ``== []`` assertion would have to be edited on every implementation.
    """
    block = registry.planner_tool_block()
    for spec in registry.all_specs():
        advertised = f"**{spec.name}**" in block
        assert advertised == spec.implemented, (
            f"{spec.name}: implemented={spec.implemented} but "
            f"advertised={advertised}"
        )


def test_implementing_an_unregistered_tool_is_an_error():
    with pytest.raises(KeyError, match="unregistered tool"):
        registry.implement("summon_kraken")(lambda: None)


def test_a_tool_cannot_be_implemented_twice():
    with pytest.raises(ValueError, match="already has an implementation"):
        registry.implement("compute_risk_score")(lambda: None)


def test_arg_validation_accepts_references_it_cannot_type_check_yet():
    assert registry.validate_args("wave_forecast", {"lat": "$s1.lat", "lon": "$s1.lon"}) == []


def test_arg_validation_finds_references_nested_in_structures():
    problems = registry.validate_args(
        "geofence_check", {"points": [{"lat": "$s1.lat", "lon": "$s1.lon"}]}
    )
    assert problems == []


def test_arg_validation_rejects_wrong_types_and_unknown_args():
    problems = registry.validate_args("wave_forecast", {"lat": "north", "lon": 79.8})
    assert any("lat" in p for p in problems)

    problems = registry.validate_args(
        "wave_forecast", {"lat": 10.0, "lon": 79.0, "depth": 30}
    )
    assert any("unexpected argument" in p for p in problems)


def test_arg_validation_reports_missing_required_args():
    problems = registry.validate_args("wave_forecast", {"lat": 10.0})
    assert any("missing required argument 'lon'" in p for p in problems)


def test_arg_validation_returns_all_problems_not_just_the_first():
    """The planner gets one replan attempt; it must see everything at once."""
    problems = registry.validate_args("wave_forecast", {"lat": "north", "bogus": 1})
    assert len(problems) >= 3  # bad type, unknown arg, missing lon


# ==========================================================================
# Plan validation
# ==========================================================================


def _plan(steps, intent="safety_assess"):
    return {"intent_type": intent, "steps": steps}


def test_valid_safety_plan_passes():
    result = validate_plan(
        fallback_plan(
            QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
        ),
        allow_unimplemented=True,
    )
    assert result.ok, result.errors
    assert result.plan is not None


def test_unknown_tool_is_rejected():
    result = validate_plan(
        _plan([{"id": "s1", "tool": "consult_oracle", "args": {}}]), allow_unimplemented=True
    )
    assert not result.ok
    assert any("unknown tool" in e for e in result.errors)


def test_unimplemented_tool_is_rejected_by_default():
    """Default path must not accept a plan it cannot execute.

    Uses whichever tool is still unimplemented, rather than naming one, so the
    test keeps testing the rule as Phase 1 fills tools in instead of having to
    be retargeted each time.
    """
    pending = [s for s in registry.all_specs() if not s.implemented]
    if not pending:
        pytest.skip("every tool is implemented; the rule no longer has a subject")

    result = validate_plan(
        _plan([{"id": "s1", "tool": pending[0].name, "args": {}}], intent="pfz_locate")
    )
    assert not result.ok
    assert any("no implementation" in e for e in result.errors)


def test_a_fully_implemented_fallback_plan_passes_without_the_escape_hatch():
    """geofence_check is the first query type wired end to end.

    It validates with allow_unimplemented left at its default, meaning every
    tool the plan calls actually exists and could run right now.
    """
    result = validate_plan(fallback_plan(QueryType.GEOFENCE_CHECK, {"PLACE": "Rameswaram"}))
    assert result.ok, result.errors


def test_bad_argument_type_is_caught_before_execution():
    result = validate_plan(
        _plan(
            [
                {"id": "s1", "tool": "wave_forecast", "args": {"lat": "north", "lon": 79.8}},
                {"id": "s2", "tool": "compute_risk_score", "args": {"vessel_class": "frp_9m"}},
            ]
        ),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert any("s1" in e and "lat" in e for e in result.errors)


def test_safety_critical_step_cannot_be_optional():
    """A safety check that may be skipped is not a safety check."""
    result = validate_plan(
        _plan(
            [
                {
                    "id": "s1",
                    "tool": "active_alerts",
                    "args": {"district": "Nagapattinam"},
                    "optional": True,
                },
                {"id": "s2", "tool": "compute_risk_score", "args": {"vessel_class": "frp_9m"}},
            ]
        ),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert any("safety-critical" in e for e in result.errors)


def test_safety_plan_must_call_compute_risk_score():
    """The verdict may not come from anywhere else."""
    result = validate_plan(
        _plan([{"id": "s1", "tool": "wave_forecast", "args": {"lat": 10.0, "lon": 79.0}}]),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert any("compute_risk_score" in e for e in result.errors)


def test_safety_plan_without_alert_check_warns():
    """Not fatal -- it produces a degraded answer -- but it must be visible."""
    result = validate_plan(
        _plan(
            [
                {"id": "s1", "tool": "wave_forecast", "args": {"lat": 10.0, "lon": 79.0}},
                {"id": "s2", "tool": "compute_risk_score", "args": {"vessel_class": "frp_9m"}},
            ]
        ),
        allow_unimplemented=True,
    )
    assert result.ok
    assert any("active_alerts" in w for w in result.warnings)


def test_causal_plan_must_not_compute_a_risk_score():
    """Explaining is not advising."""
    result = validate_plan(
        _plan(
            [{"id": "s1", "tool": "compute_risk_score", "args": {"vessel_class": "frp_9m"}}],
            intent="causal_explain",
        ),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert any("Explaining is not advising" in e for e in result.errors)


def test_reference_to_a_field_the_tool_does_not_return_is_caught():
    """Otherwise this surfaces at runtime as a confusing null."""
    result = validate_plan(
        _plan(
            [
                {"id": "s1", "tool": "wave_forecast", "args": {"lat": 10.0, "lon": 79.0}},
                {
                    "id": "s2",
                    "tool": "compute_risk_score",
                    "args": {"vessel_class": "frp_9m", "wind_speed": "$s1.gust_kn"},
                    "depends_on": ["s1"],
                },
            ]
        ),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert any("returns no field 'gust_kn'" in e for e in result.errors)


def test_structural_failures_are_reported_without_a_plan():
    result = validate_plan(
        _plan(
            [
                {"id": "s1", "tool": "wave_forecast", "args": {}, "depends_on": ["s2"]},
                {"id": "s2", "tool": "wind_forecast", "args": {}, "depends_on": ["s1"]},
            ]
        ),
        allow_unimplemented=True,
    )
    assert not result.ok
    assert result.plan is None
    assert any("cycle" in e.lower() for e in result.errors)


def test_error_feedback_is_phrased_for_a_prompt():
    result = validate_plan(
        _plan([{"id": "s1", "tool": "consult_oracle", "args": {}}]), allow_unimplemented=True
    )
    feedback = result.error_feedback()
    assert feedback.startswith("The plan you produced is invalid.")
    assert "1." in feedback


# ==========================================================================
# Fallback plans -- the demo's floor
# ==========================================================================


@pytest.mark.parametrize("intent", list(QueryType))
def test_every_query_type_has_a_valid_fallback_plan(intent):
    """If the planner LLM dies at the venue, these still answer all four."""
    subs = {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
    result = validate_plan(fallback_plan(intent, subs), allow_unimplemented=True)
    assert result.ok, f"{intent.value} fallback is invalid: {result.errors}"


def test_fallback_plans_exist_for_all_four_query_types():
    assert set(FALLBACK_PLANS) == set(QueryType)


def test_fallback_plan_refuses_to_build_with_a_missing_substitution():
    """An unfilled $PLACE must not reach the executor as a literal string."""
    with pytest.raises(KeyError, match="VESSEL_CLASS"):
        fallback_plan(QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam"})


def test_fallback_plans_can_be_layered_for_parallel_execution():
    subs = {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
    plan = validate_plan(
        fallback_plan(QueryType.SAFETY_ASSESS, subs), allow_unimplemented=True
    ).plan
    layers = plan.execution_layers()
    assert layers[0] == ["s1"]
    # The four weather calls fan out together once the place is resolved.
    assert set(layers[1]) == {"s2", "s3", "s4", "s5"}
    assert layers[-1] == ["s6"]
