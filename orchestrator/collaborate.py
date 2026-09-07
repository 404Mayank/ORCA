"""The collaboration loop: agents review results and extend the plan.

This is what makes ORCA a multi-agent system rather than a pipeline with
several modules in it.

    plan -> execute -> REVIEW -> extend -> execute -> REVIEW -> ... -> answer

After each wave, every agent sees what came back and may ask another agent, by
name, for something it now knows it needs. Those requests become plan steps,
go through the *same* ``validate_plan()`` as the original plan, and run in the
next wave. The plan is no longer fixed at validation time -- it grows because
an agent decided it should.

---------------------------------------------------------------------------
WHY RE-EXECUTE RATHER THAN APPEND TO A RUNNING DAG
---------------------------------------------------------------------------

Each wave runs the *whole* extended plan from scratch into a fresh log, rather
than splicing new steps into a finished execution. Three reasons, in order of
how much they matter:

1.  **The verifier walks one coherent log.** Stitching two logs together means
    two id sequences, two clocks, and a claim that cites ``tc_003`` from the
    wrong wave. The audit trail is the product here; a seam in it is not worth
    the saving.
2.  **Tools never fetch** -- they read a cache -- so a full re-run is tens of
    milliseconds, not a network round trip. Re-execution is genuinely cheap and
    it is cheap *because* of the ingest rule, which is a nice payoff for having
    followed it.
3.  Re-validating the whole plan each wave means an agent's addition cannot
    quietly break an invariant that held before it, such as a safety-critical
    step becoming optional.

---------------------------------------------------------------------------
BOUNDS, BECAUSE AN UNBOUNDED AGENT LOOP IS A HANG
---------------------------------------------------------------------------

* At most :data:`MAX_ROUNDS` review passes. Two is enough for the real cases:
  a finding produces a question, and the answer to that question occasionally
  produces one more.
* A request whose ``(tool, args)`` already appears in the plan is dropped.
  Without this, an agent that asks for a geofence check every time it sees a
  candidate zone asks forever, because the candidate is still there next round.
* A hard cap on added steps. When it is reached, non-critical requests are
  dropped first and the fact is recorded -- never silently.
* If the extended plan fails validation, the previous wave's result stands.
  A collaboration round can improve an answer; it may never destroy one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from agents.base import AgentRequest, all_agents
from agents.deliberate import Deliberation, deliberate
from core.schemas.intent import Intent, QueryType
from core.schemas.plan import Plan
from orchestrator.executor import ExecutionResult, execute_plan_sync
from orchestrator.progress import ProgressBus
from orchestrator.validate_plan import validate_plan

__all__ = [
    "MAX_ROUNDS",
    "CollaborationResult",
    "deliberating_enabled",
    "max_added_steps",
    "max_rounds",
    "run_with_collaboration",
    "set_deliberating",
    "set_max_added_steps",
    "set_max_rounds",
]

#: Review passes after the first execution.
MAX_ROUNDS = 2

#: Ceiling on steps added by collaboration, on top of the planner's own.
#: ``Plan`` caps the total at 12; this keeps agents from consuming the whole
#: budget and leaving no room for a replan.
MAX_ADDED_STEPS = 4

# Runtime overrides set by POST /settings. None means the compiled defaults
# above win. Same pattern as the deliberation override in this module: a
# process restart always returns to the defaults.
_MAX_ROUNDS_OVERRIDE: int | None = None
_MAX_ADDED_STEPS_OVERRIDE: int | None = None


def max_rounds() -> int:
    """Effective collaboration rounds. Override wins; default 2."""
    if _MAX_ROUNDS_OVERRIDE is not None:
        return _MAX_ROUNDS_OVERRIDE
    return MAX_ROUNDS


def set_max_rounds(value: int | None) -> int:
    """Set or clear the max-rounds override. Returns the effective value."""
    global _MAX_ROUNDS_OVERRIDE
    _MAX_ROUNDS_OVERRIDE = value
    return max_rounds()


def max_added_steps() -> int:
    """Effective added-steps budget. Override wins; default 4."""
    if _MAX_ADDED_STEPS_OVERRIDE is not None:
        return _MAX_ADDED_STEPS_OVERRIDE
    return MAX_ADDED_STEPS


def set_max_added_steps(value: int | None) -> int:
    """Set or clear the added-steps override. Returns the effective value."""
    global _MAX_ADDED_STEPS_OVERRIDE
    _MAX_ADDED_STEPS_OVERRIDE = value
    return max_added_steps()

# Runtime override set by POST /settings. None means the default (on).
# Same pattern as the tier override in orchestrator/llm/client.py: a process
# restart always returns to the default. Off skips the per-agent LLM fan-out
# and runs the rule floor alone -- faster, fewer follow-up checks.
_DELIBERATING_OVERRIDE: bool | None = None


def deliberating_enabled() -> bool:
    """Whether agents deliberate this turn. Override wins; default True."""
    if _DELIBERATING_OVERRIDE is not None:
        return _DELIBERATING_OVERRIDE
    return True


def set_deliberating(value: bool | None) -> bool:
    """Set or clear the deliberation override. Returns the effective value."""
    global _DELIBERATING_OVERRIDE
    _DELIBERATING_OVERRIDE = value
    return deliberating_enabled()


@dataclass
class CollaborationResult:
    """The final execution plus a record of how the plan grew."""

    result: ExecutionResult
    plan: Plan
    rounds: int = 0
    requests: list[AgentRequest] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: One per agent that actually reasoned this turn.
    deliberations: list[Deliberation] = field(default_factory=list)

    @property
    def concerns(self) -> list[str]:
        """Caveats the agents raised, in words. Numbers already stripped."""
        seen, out = set(), []
        for d in self.deliberations:
            for concern in d.concerns:
                if concern and concern not in seen:
                    seen.add(concern)
                    out.append(concern)
        return out

    @property
    def reasoned(self) -> bool:
        return any(d.used_llm for d in self.deliberations)

    @property
    def collaborated(self) -> bool:
        return bool(self.requests)

    def describe(self) -> list[str]:
        """One line per accepted request, for the trace and the UI."""
        return [request.describe() for request in self.requests]


def _existing_signatures(steps: list[dict[str, Any]]) -> set[tuple[str, str]]:
    import json

    return {
        (step["tool"], json.dumps(step.get("args", {}), sort_keys=True, default=str))
        for step in steps
    }


def _plan_to_raw(plan: Plan) -> dict[str, Any]:
    """A Plan back into the raw dict shape, so steps can be appended."""
    return {
        "intent_type": plan.intent_type.value,
        "is_fallback": plan.is_fallback,
        "steps": [
            {
                "id": step.id,
                "tool": step.tool,
                "args": step.args,
                "depends_on": list(step.depends_on),
                "agent": step.agent,
                "optional": step.optional,
            }
            for step in plan.steps
        ],
    }


def _next_step_number(steps: list[dict[str, Any]]) -> int:
    return max((int(step["id"][1:]) for step in steps), default=0) + 1


def _step_for(request: AgentRequest, number: int) -> dict[str, Any]:
    """One agent request as a raw plan step.

    ``optional`` follows the **registry**, not the request. Two different
    things were called "critical" and conflating them was a real bug:

    * ``AgentRequest.critical`` means *this came from the rule floor, do not
      drop it under budget pressure*. A deliberating agent may never set it.
    * ``ToolSpec.safety_critical`` means *a failure of this tool must degrade
      the verdict rather than be skipped*. It is a property of the tool.

    Deriving ``optional`` from the request meant every LLM-proposed call to a
    safety-critical tool came out optional, and ``validate_plan()`` rejected
    the whole extension -- so an agent correctly asking for a geofence check
    had its request thrown out on a technicality. The tool decides.
    """
    from tools import registry

    tool_is_safety_critical = (
        registry.has(request.tool) and registry.get(request.tool).safety_critical
    )
    return {
        "id": f"s{number}",
        "tool": request.tool,
        "args": request.args,
        # Dependencies are already encoded as literal values by the requesting
        # agent -- it had the outputs in hand when it asked -- so a
        # collaboration step depends on nothing and can run immediately.
        "depends_on": [],
        "agent": request.to_agent,
        "optional": False if tool_is_safety_critical else not request.critical,
    }


def _step_for(request: AgentRequest, number: int) -> dict[str, Any]:
    """One agent request as a raw plan step.

    ``optional`` follows the **registry**, not the request. Two different
    things were called "critical" and conflating them was a real bug:

    * ``AgentRequest.critical`` means *this came from the rule floor, do not
      drop it under budget pressure*. A deliberating agent may never set it.
    * ``ToolSpec.safety_critical`` means *a failure of this tool must degrade
      the verdict rather than be skipped*. It is a property of the tool.

    Deriving ``optional`` from the request meant every LLM-proposed call to a
    safety-critical tool came out optional, and ``validate_plan()`` rejected
    the whole extension -- so an agent correctly asking for a geofence check
    had its request thrown out on a technicality. The tool decides.
    """
    from tools import registry

    tool_is_safety_critical = (
        registry.has(request.tool) and registry.get(request.tool).safety_critical
    )
    return {
        "id": f"s{number}",
        "tool": request.tool,
        "args": request.args,
        # Dependencies are already encoded as literal values by the requesting
        # agent -- it had the outputs in hand when it asked -- so a
        # collaboration step depends on nothing and can run immediately.
        "depends_on": [],
        "agent": request.to_agent,
        "optional": False if tool_is_safety_critical else not request.critical,
    }


def _deliberate_all(
    pending: list,
    result: ExecutionResult,
    intent: Intent,
    progress: ProgressBus | None = None,
) -> list[Deliberation]:
    """One deliberation call per agent, concurrently, in agent order.

    Deliberations are independent -- each agent reads the same finished
    result and writes only its own Deliberation -- so sequential calls just
    stack model latencies (measured 2026-09-07: ~6 s per call on the free
    tier, seconds apart). Threads share nothing mutable here: llm.complete
    is urllib I/O (GIL released), the registry is read-only on this path.
    pool.map preserves agent order, so traces read deterministically.
    """
    if not pending:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(pending)), thread_name_prefix="orca-deliberate") as pool:
        thoughts = list(pool.map(lambda agent: deliberate(agent, result, intent), pending))
    # One event per finished deliberation. Assessment text is already
    # number-stripped at the source; cap length defensively anyway.
    if progress is not None:
        for thought in thoughts:
            progress.emit(
                "deliberate",
                agent=thought.agent,
                used_llm=bool(thought.used_llm),
                assessment=(thought.assessment or "")[:300],
            )
    return thoughts


def run_with_collaboration(
    plan: Plan,
    intent: Intent,
    turn_id: str = "t_001",
    deliberating: bool = True,
    progress: ProgressBus | None = None,
) -> CollaborationResult:
    """Execute a plan, then let the agents extend it until they are satisfied.

    Each agent contributes twice per round: its **rule floor** (``review()``,
    where the safety-critical requests live) and its **own reasoning**
    (``deliberate()``, one LLM call in which it decides what it is missing).
    The model may add requests; it may never remove one the rules produced.

    ``deliberating=False`` runs the rules alone -- used by tests, and the
    automatic behaviour when no provider is reachable. The parameter is now
    test-only in production: turn.py never passes it, so the module-global
    gate (deliberating_enabled, set via POST /settings) is what matters.

    Never raises and never returns a worse result than a single execution: if a
    round produces nothing valid, the previous wave's result is what is
    returned.
    """
    result = execute_plan_sync(plan, turn_id=turn_id)
    outcome = CollaborationResult(result=result, plan=plan)

    raw = _plan_to_raw(plan)
    added = 0

    # Deliberation runs in round one only. A second LLM pass over results the
    # agent has already reasoned about produces restatements, not new
    # questions, and costs a call per agent to do it.
    for round_number in range(1, max_rounds() + 1):
        seen = _existing_signatures(raw["steps"])
        requests: list[AgentRequest] = []
        pending: list = []  # agents to deliberate this round (round 1 only)

        for agent in all_agents():
            # 1. The rule floor. Safety-critical requests live here and are not
            #    subject to a model's judgement on the day: forgetting to
            #    geofence a candidate zone means a fisherman arrested in Sri
            #    Lankan waters, so that check fires whether or not an LLM
            #    thinks of it.
            try:
                requests.extend(agent.review(outcome.result, intent))
            except Exception as exc:  # noqa: BLE001 -- one agent must not stop the turn
                outcome.notes.append(
                    f"{agent.name}.review raised {type(exc).__name__}: {exc}"
                )

            # 2. The agent's own reasoning, which may only ADD to the floor.
            #    Deliberation is optional throughout: no key, a timeout, a rate
            #    limit or unparseable output all leave the rules standing.
            # Only agents that actually ran something have anything to reason
            # about, and each deliberation costs an LLM call -- six calls a
            # turn exhausts a free tier quickly. An idle agent is skipped
            # silently rather than asked to comment on an empty result.
            ran_something = bool(agent.fragment(outcome.result, intent).step_ids)

            # Read-only reports (conditions) carry no verdict and no vessel
            # gate, so there is no safety-critical request deliberation could
            # add -- and each deliberation costs a queued model call (measured
            # 2026-09-07: the deliberation slice dominated turn latency). The
            # rule floor above still runs; only the LLM fan-out is skipped.
            # Deliberating agents lose their `assessed:` trace lines on these
            # turns; that is the trade, stated here rather than discovered.
            if (
                deliberating
                and deliberating_enabled()
                and round_number == 1
                and ran_something
                and intent.query_type is not QueryType.CONDITIONS_REPORT
            ):
                pending.append(agent)

        # The fan-out: concurrent deliberation, merged back in agent order.
        # Rule-floor requests above are already in `requests`; these append.
        for agent, thought in zip(
            pending, _deliberate_all(pending, outcome.result, intent, progress)
        ):
            outcome.deliberations.append(thought)
            if thought.ok:
                requests.extend(thought.requests)
                if thought.assessment:
                    outcome.notes.append(f"{agent.name} assessed: {thought.assessment}")
            else:
                outcome.notes.append(
                    f"{agent.name} did not deliberate ({thought.error}); "
                    "rule-based requests still applied"
                )

        # Critical requests first, so the step budget is spent on them.
        requests.sort(key=lambda r: not r.critical)

        accepted: list[AgentRequest] = []
        for request in requests:
            if request.signature in seen:
                continue  # already planned, or asked for last round
            if added + len(accepted) >= max_added_steps():
                outcome.dropped.append(
                    f"{request.describe()} [step budget reached]"
                )
                continue
            seen.add(request.signature)
            accepted.append(request)

        if not accepted:
            outcome.rounds = round_number - 1
            break

        next_id = _next_step_number(raw["steps"])
        for offset, request in enumerate(accepted):
            raw["steps"].append(_step_for(request, next_id + offset))

        validated = validate_plan(raw)

        if not validated.ok:
            # One malformed request must not cost the round its good ones.
            #
            # LLM-proposed requests and the rule floor are validated as a
            # batch, so a single bad argument from a deliberating agent used to
            # sink the whole extension -- including the safety-critical
            # geofence check the rules had produced correctly. The floor is
            # precisely what must never be lost, so drop the model's additions
            # and retry with the rules alone.
            floor = [request for request in accepted if request.critical]
            if floor and len(floor) < len(accepted):
                outcome.notes.append(
                    f"round {round_number}: extension rejected "
                    f"({validated.errors[:1]}); retrying with the "
                    f"{len(floor)} rule-based request(s) alone"
                )
                del raw["steps"][-len(accepted):]
                for offset, request in enumerate(floor):
                    raw["steps"].append(_step_for(request, next_id + offset))
                accepted = floor
                validated = validate_plan(raw)

        if not validated.ok:
            # Even the floor did not validate. Keep the last good result rather
            # than losing an answer to an over-eager agent.
            outcome.notes.append(
                f"round {round_number}: extension rejected, keeping previous result "
                f"({validated.errors[:2]})"
            )
            del raw["steps"][-len(accepted):]
            outcome.rounds = round_number - 1
            break

        outcome.result = execute_plan_sync(validated.plan, turn_id=turn_id)
        outcome.plan = validated.plan
        outcome.requests.extend(accepted)
        if progress is not None:
            progress.emit("collaborate", round=round_number, added=len(accepted))
        outcome.notes.extend(
            f"round {round_number}: {request.describe()}" for request in accepted
        )
        added += len(accepted)
        outcome.rounds = round_number

    return outcome
