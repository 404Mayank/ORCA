"""The task DAG the planner emits, and the reference syntax that binds it.

Second of the two blocks returned by the single planner LLM call.

A plan is a list of steps with dependencies. It is *data*, not code, which is
the entire reason ``orchestrator/validate_plan.py`` can check it before a single
tool runs. The LLM writes the plan; nothing executes until deterministic code
has agreed the plan is well-formed.

The governing rule constrains what a plan may contain. A step says
"call ``compute_risk_score`` with these inputs". A step never says "the swell is
too high for this vessel" -- planning is not adjudication. The planner arranges
the calculation; it does not perform it.

**Reference syntax.** A step argument may reference an earlier step's output
with ``$sN`` for the whole output or ``$sN.field.subfield`` for part of it::

    {"id": "s7", "tool": "compute_risk_score",
     "args": {"wave": "$s3", "wind": "$s4.speed_kn"},
     "depends_on": ["s3", "s4"]}

References and ``depends_on`` must agree. They are stated twice on purpose:
the executor needs the edges, and stating them redundantly means
``validate_plan()`` can catch a plan whose data flow and declared ordering
disagree -- a class of bug that otherwise shows up as a null at runtime.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.schemas.intent import QueryType

__all__ = [
    "STEP_ID_PATTERN",
    "REFERENCE_PATTERN",
    "MAX_PLAN_STEPS",
    "PlanStep",
    "Plan",
    "extract_references",
    "parse_reference",
]

#: Step ids are ``s`` followed by digits. Constrained so that references are
#: unambiguous to parse and cheap to validate.
STEP_ID_PATTERN = re.compile(r"^s\d+$")

#: ``$s3`` or ``$s3.speed_kn`` or ``$s3.stats.mean``.
REFERENCE_PATTERN = re.compile(r"^\$(s\d+)((?:\.[A-Za-z_][A-Za-z0-9_]*)*)$")

#: Hard cap from CLAUDE.md. A plan longer than this is a planner that has lost
#: the thread, and running it wastes the demo clock.
MAX_PLAN_STEPS = 12


def parse_reference(value: str) -> tuple[str, list[str]] | None:
    """Split a reference into its step id and field path.

    Returns ``("s3", ["stats", "mean"])`` for ``"$s3.stats.mean"``, or None if
    the string is not a reference at all.
    """
    if not isinstance(value, str) or not value.startswith("$"):
        return None
    match = REFERENCE_PATTERN.match(value)
    if match is None:
        return None
    step_id, path = match.groups()
    fields = [f for f in path.split(".") if f]
    return step_id, fields


def extract_references(args: Any) -> list[tuple[str, list[str]]]:
    """Find every ``$sN`` reference nested anywhere in an argument structure.

    Walks dicts and lists, because a reference can legitimately sit inside a
    list of points or a nested options object.
    """
    found: list[tuple[str, list[str]]] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            ref = parse_reference(node)
            if ref is not None:
                found.append(ref)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(args)
    return found


class PlanStep(BaseModel):
    """One tool invocation.

    ``args`` is deliberately untyped here. It is validated against the tool's
    own Pydantic input schema by ``validate_plan()``, using
    ``tools/registry.py`` -- which is the only place that knows what each tool
    accepts. Duplicating tool signatures into this module would guarantee
    drift, exactly as hand-maintaining a tool list in a prompt would.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Step id, e.g. 's3'.")
    tool: str = Field(min_length=1, description="Must exist in tools/registry.py.")
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    agent: str | None = Field(
        default=None,
        description=(
            "Which agent group owns this step -- ocean, weather, geospatial, "
            "risk. Used to fan out execution in parallel. Advisory only; the "
            "DAG edges are what actually constrain ordering."
        ),
    )
    optional: bool = Field(
        default=False,
        description=(
            "If True, a failure degrades the answer instead of failing it. "
            "Safety-critical steps must never be optional -- a missing alert "
            "check downgrades the verdict, it does not get skipped quietly."
        ),
    )
    timeout_s: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _check_id_and_refs(self) -> PlanStep:
        if not STEP_ID_PATTERN.match(self.id):
            raise ValueError(f"Step id {self.id!r} must match 'sN', e.g. 's3'.")
        for dep in self.depends_on:
            if not STEP_ID_PATTERN.match(dep):
                raise ValueError(f"depends_on entry {dep!r} in step {self.id} is not a step id.")
        if self.id in self.depends_on:
            raise ValueError(f"Step {self.id} depends on itself.")

        # Any string that looks like it was meant to be a reference but does
        # not parse is caught here rather than reaching the executor as a
        # literal "$s3." string argument.
        def check_malformed(node: Any) -> None:
            if isinstance(node, str) and node.startswith("$") and parse_reference(node) is None:
                raise ValueError(
                    f"Step {self.id} argument {node!r} looks like a reference but "
                    "does not match $sN or $sN.field."
                )
            if isinstance(node, dict):
                for v in node.values():
                    check_malformed(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    check_malformed(v)

        check_malformed(self.args)
        return self

    def referenced_steps(self) -> set[str]:
        """Step ids this step's arguments actually read from."""
        return {step_id for step_id, _ in extract_references(self.args)}


class Plan(BaseModel):
    """The DAG. Structural checks live here; semantic checks live in
    ``orchestrator/validate_plan.py``.

    The split is deliberate. This model enforces what can be known from the
    plan alone -- unique ids, resolvable references, no cycles, under the cap.
    ``validate_plan()`` adds what needs the registry: that each tool exists and
    that each argument matches that tool's schema. Keeping the registry out of
    ``core/schemas`` preserves the rule that this package imports from nothing.
    """

    model_config = ConfigDict(extra="forbid")

    intent_type: QueryType
    steps: list[PlanStep] = Field(min_length=1)
    rationale: str | None = Field(
        default=None, description="One line from the planner. Never shown to the user."
    )
    is_fallback: bool = Field(
        default=False,
        description="True when this is the hardcoded plan used after replanning failed.",
    )

    @model_validator(mode="after")
    def _structural_checks(self) -> Plan:
        if len(self.steps) > MAX_PLAN_STEPS:
            raise ValueError(
                f"Plan has {len(self.steps)} steps, over the cap of {MAX_PLAN_STEPS}."
            )

        ids = [s.id for s in self.steps]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate step ids: {sorted(duplicates)}.")
        id_set = set(ids)

        for step in self.steps:
            missing = [d for d in step.depends_on if d not in id_set]
            if missing:
                raise ValueError(f"Step {step.id} depends on unknown steps {missing}.")

            refs = step.referenced_steps()
            unknown_refs = refs - id_set
            if unknown_refs:
                raise ValueError(
                    f"Step {step.id} references unknown steps {sorted(unknown_refs)}."
                )
            # Data flow and declared ordering must agree.
            undeclared = refs - set(step.depends_on)
            if undeclared:
                raise ValueError(
                    f"Step {step.id} reads from {sorted(undeclared)} but does not "
                    "declare them in depends_on. The executor orders by "
                    "depends_on, so this would read a value that does not exist yet."
                )

        cycle = self._find_cycle()
        if cycle:
            raise ValueError(f"Plan contains a cycle: {' -> '.join(cycle)}.")
        return self

    def _find_cycle(self) -> list[str] | None:
        """Depth-first search returning the first cycle found, for the message.

        Naming the actual cycle matters: "s3 -> s5 -> s3" tells the planner
        what to fix on the one replan attempt it gets.
        """
        graph = {s.id: list(s.depends_on) for s in self.steps}
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(graph, WHITE)
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            colour[node] = GREY
            stack.append(node)
            for nxt in graph.get(node, []):
                if colour.get(nxt) == GREY:
                    return stack[stack.index(nxt):] + [nxt]
                if colour.get(nxt) == WHITE:
                    found = visit(nxt)
                    if found:
                        return found
            stack.pop()
            colour[node] = BLACK
            return None

        for node in graph:
            if colour[node] == WHITE:
                found = visit(node)
                if found:
                    return found
        return None

    def step_by_id(self) -> dict[str, PlanStep]:
        return {s.id: s for s in self.steps}

    def execution_layers(self) -> list[list[str]]:
        """Group steps into waves that may run in parallel.

        Each returned list can be dispatched concurrently; the next list waits
        for it. This is what lets the four agent groups run in parallel where
        dependencies allow, per the architecture diagram.
        """
        remaining = {s.id: set(s.depends_on) for s in self.steps}
        layers: list[list[str]] = []
        done: set[str] = set()

        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if deps <= done)
            if not ready:
                # Unreachable: the cycle check above runs first.
                raise ValueError(f"Deadlock in plan; unresolved: {sorted(remaining)}.")
            layers.append(ready)
            done.update(ready)
            for sid in ready:
                del remaining[sid]
        return layers
