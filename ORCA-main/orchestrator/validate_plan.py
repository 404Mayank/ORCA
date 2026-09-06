"""Deterministic plan validation. Runs before any tool executes.

From CLAUDE.md: every tool exists in the registry; every argument matches its
Pydantic schema; no cycles; all ``depends_on`` ids exist; every ``$sN``
reference resolves; step count under the cap. On failure, one replan attempt
with the error fed back, then a hardcoded fallback plan for that intent type.

The structural half of that list lives in ``core.schemas.plan.Plan`` and runs
at parse time. This module adds the half that needs the registry, plus two
rules that are ours rather than the brief's:

* A **safety-critical tool may not be marked optional.** Skipping the alert
  check quietly is how a missing cyclone warning turns into a clean verdict.
* A plan may not call an **unimplemented** tool. The registry does not
  advertise them, so a plan containing one means the planner invented it.

Errors are collected, never raised one at a time. The planner gets a single
replan attempt, and spending it fixing one of five problems wastes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

import tools  # noqa: F401  -- import for side effect: populates the registry
from core.schemas.intent import QueryType
from core.schemas.plan import Plan, extract_references, parse_reference
from tools import registry

__all__ = [
    "PlanValidationResult",
    "validate_plan",
    "fallback_plan",
    "FALLBACK_PLANS",
]

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class PlanValidationResult:
    """Outcome of validation. ``ok`` is the only thing the caller must check."""

    ok: bool
    plan: Plan | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error_feedback(self) -> str:
        """The message handed back to the planner for its one replan attempt.

        Phrased as instructions rather than as a stack trace, because it is
        going into a prompt.
        """
        lines = ["The plan you produced is invalid. Fix these and return a new plan:"]
        lines += [f"  {i}. {e}" for i, e in enumerate(self.errors, 1)]
        return "\n".join(lines)


def validate_plan(
    raw: dict[str, Any] | Plan,
    *,
    allow_unimplemented: bool = False,
) -> PlanValidationResult:
    """Validate a planner-produced plan.

    ``allow_unimplemented`` exists for Phase 0 only, where no tool has a
    function yet but the plan shape is still worth checking. It defaults to
    False so that the production path cannot accidentally accept a plan it
    cannot run.
    """
    errors: list[str] = []
    warnings: list[str] = []

    # -- structural: parse into Plan, which runs the DAG checks --------
    if isinstance(raw, Plan):
        plan = raw
    else:
        try:
            plan = Plan.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "plan"
                errors.append(f"{loc}: {err['msg']}")
            return PlanValidationResult(ok=False, plan=None, errors=errors)

    # -- semantic: needs the registry ----------------------------------
    for step in plan.steps:
        if not registry.has(step.tool):
            errors.append(
                f"step {step.id}: unknown tool {step.tool!r}. "
                f"Available: {sorted(s.name for s in registry.all_specs())}"
            )
            continue

        spec = registry.get(step.tool)

        if not spec.implemented and not allow_unimplemented:
            errors.append(
                f"step {step.id}: tool {step.tool!r} is registered but has no "
                "implementation, so it was not offered to you. Do not call it."
            )

        errors.extend(f"step {step.id}: {p}" for p in registry.validate_args(step.tool, step.args))

        if spec.safety_critical and step.optional:
            errors.append(
                f"step {step.id}: {step.tool!r} is safety-critical and cannot be "
                "optional. A safety check that is allowed to be skipped is not a "
                "safety check."
            )

    errors.extend(_check_reference_fields(plan))

    # -- advisory ------------------------------------------------------
    if plan.intent_type is QueryType.SAFETY_ASSESS:
        called = {s.tool for s in plan.steps}
        if "compute_risk_score" not in called:
            errors.append(
                "a safety_assess plan must call compute_risk_score; the verdict "
                "may not come from anywhere else."
            )
        if "active_alerts" not in called:
            warnings.append(
                "no active_alerts step: the answer will be degraded and cannot "
                "reach a 'go' verdict."
            )

    if plan.intent_type is QueryType.CAUSAL_EXPLAIN and "compute_risk_score" in {
        s.tool for s in plan.steps
    }:
        errors.append(
            "a causal_explain plan must not call compute_risk_score. Explaining "
            "is not advising."
        )

    return PlanValidationResult(
        ok=not errors, plan=plan if not errors else None, errors=errors, warnings=warnings
    )


def _check_reference_fields(plan: Plan) -> list[str]:
    """Check that ``$sN.field`` names a field the producing tool actually returns.

    ``Plan`` already proved the step id resolves. This goes one level further:
    if step 4 reads ``$s3.gust_kn`` but ``wave_forecast`` has no ``gust_kn``,
    the executor would hand step 4 a None and the failure would surface much
    later as a confusing null rather than as a planning error.

    Only the first path segment is checked. Deeper paths reach into nested
    models and item types, and following them would mean reimplementing
    attribute resolution for marginal benefit.
    """
    problems: list[str] = []
    by_id = plan.step_by_id()

    for step in plan.steps:
        for ref_step_id, path in extract_references(step.args):
            if not path:
                continue
            producer = by_id.get(ref_step_id)
            if producer is None or not registry.has(producer.tool):
                continue
            out_fields = registry.get(producer.tool).output_model.model_fields
            head = path[0]
            if head not in out_fields:
                problems.append(
                    f"step {step.id}: references ${ref_step_id}.{'.'.join(path)}, but "
                    f"{producer.tool!r} returns no field {head!r}. "
                    f"It returns: {sorted(out_fields)}"
                )
    return problems


# --------------------------------------------------------------------------
# Hardcoded fallback plans
# --------------------------------------------------------------------------
# Used after one failed replan. These are the demo's floor: if the planner LLM
# is unavailable, confused, or the venue has lost internet, these still answer
# the four query types. They are deliberately boring and fully wired.


def _safety_plan() -> dict[str, Any]:
    return {
        "intent_type": "safety_assess",
        "is_fallback": True,
        "rationale": "hardcoded fallback",
        "steps": [
            {"id": "s1", "tool": "resolve_place", "args": {"name": "$PLACE"}, "agent": "geospatial"},
            {
                "id": "s2",
                "tool": "wave_forecast",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon", "hours": 24},
                "depends_on": ["s1"],
                "agent": "weather",
            },
            {
                "id": "s3",
                "tool": "wind_forecast",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon", "hours": 24},
                "depends_on": ["s1"],
                "agent": "weather",
            },
            {
                "id": "s4",
                "tool": "tides",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon", "hours": 24},
                "depends_on": ["s1"],
                "agent": "weather",
            },
            {
                "id": "s5",
                "tool": "active_alerts",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon"},
                "depends_on": ["s1"],
                "agent": "weather",
            },
            {
                "id": "s6",
                "tool": "compute_risk_score",
                "args": {
                    "vessel_class": "$VESSEL_CLASS",
                    "wave_height": "$s2.significant_wave_height",
                    "wind_speed": "$s3.wind_speed",
                    # Read from the alert step rather than asserted. A plan
                    # that hardcoded True would tell the risk function the
                    # cyclone check succeeded even when it did not.
                    "visibility": "$s3.visibility",
                    "wave_steepness": "$s2.max_steepness",
                    "alerts_checked": "$s5.checked",
                    "alerts_active": "$s5.count",
                },
                "depends_on": ["s2", "s3", "s5"],
                "agent": "risk",
            },
        ],
    }


def _pfz_plan() -> dict[str, Any]:
    return {
        "intent_type": "pfz_locate",
        "is_fallback": True,
        "steps": [
            {"id": "s1", "tool": "resolve_place", "args": {"name": "$PLACE"}, "agent": "geospatial"},
            {
                "id": "s2",
                "tool": "pfz_candidates",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon", "max_distance_km": 60.0},
                "depends_on": ["s1"],
                "agent": "ocean",
            },
            {
                "id": "s3",
                "tool": "geofence_check",
                "args": {"points": [{"lat": "$s1.lat", "lon": "$s1.lon"}]},
                "depends_on": ["s1"],
                "agent": "geospatial",
            },
        ],
    }


def _geofence_plan() -> dict[str, Any]:
    return {
        "intent_type": "geofence_check",
        "is_fallback": True,
        "steps": [
            {"id": "s1", "tool": "resolve_place", "args": {"name": "$PLACE"}, "agent": "geospatial"},
            {
                "id": "s2",
                "tool": "geofence_check",
                "args": {"points": [{"lat": "$s1.lat", "lon": "$s1.lon"}], "buffer_km": 5.0},
                "depends_on": ["s1"],
                "agent": "geospatial",
            },
        ],
    }


def _causal_plan() -> dict[str, Any]:
    return {
        "intent_type": "causal_explain",
        "is_fallback": True,
        "steps": [
            {"id": "s1", "tool": "resolve_place", "args": {"name": "$PLACE"}, "agent": "geospatial"},
            {
                "id": "s2",
                "tool": "chl_anomaly",
                "args": {"lat": "$s1.lat", "lon": "$s1.lon", "radius_km": 25.0},
                "depends_on": ["s1"],
                "agent": "ocean",
            },
            {
                "id": "s3",
                "tool": "thermal_front",
                "args": {"bbox": [78.5, 8.0, 82.0, 12.0]},
                "agent": "ocean",
            },
        ],
    }


#: Placeholders like ``$PLACE`` are substituted from the resolved intent before
#: validation. They are not step references -- ``$sN`` is the only reference
#: syntax -- so they must be filled in, or ``Plan`` will reject them as
#: malformed references. That rejection is intentional: it means an unfilled
#: fallback plan cannot execute.
FALLBACK_PLANS = {
    QueryType.SAFETY_ASSESS: _safety_plan,
    QueryType.PFZ_LOCATE: _pfz_plan,
    QueryType.GEOFENCE_CHECK: _geofence_plan,
    QueryType.CAUSAL_EXPLAIN: _causal_plan,
}


def fallback_plan(intent_type: QueryType, substitutions: dict[str, Any]) -> dict[str, Any]:
    """Build a hardcoded plan with intent values substituted in.

    ``substitutions`` maps placeholder names to values, e.g.
    ``{"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}``. Every placeholder
    must be supplied; a leftover ``$PLACE`` would be rejected by ``Plan`` as a
    malformed reference, which is the correct outcome but a confusing message.
    """
    raw = FALLBACK_PLANS[intent_type]()
    missing: list[str] = []

    def sub(node: Any) -> Any:
        if isinstance(node, str) and node.startswith("$"):
            if parse_reference(node) is not None:
                return node  # a real $sN step reference; leave it alone
            key = node[1:]
            if key in substitutions:
                return substitutions[key]
            missing.append(key)
            return node
        if isinstance(node, dict):
            return {k: sub(v) for k, v in node.items()}
        if isinstance(node, list):
            return [sub(v) for v in node]
        return node

    out = sub(raw)
    if missing:
        raise KeyError(
            f"fallback_plan for {intent_type.value} needs substitutions for "
            f"{sorted(set(missing))}; got {sorted(substitutions)}."
        )
    return out
