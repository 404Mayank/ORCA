"""The domain-agent contract.

**None of the agents built on this base call an LLM, and that is the point.**

CLAUDE.md names seven agents. Two of them reason in language -- the planner
decides what to do, the narrator decides how to say it -- and both already
exist. The other four (ocean, weather, geospatial, risk) do a different job:
they retrieve, compute and threshold. The governing rule puts that job in code:

    The LLM interprets, plans, asks, hypothesises and narrates.
    Code retrieves, computes, thresholds and verifies.

So ``agents/prompts/ocean.md``, ``weather.md``, ``geo.md`` and ``risk.md`` are
empty, and they should stay empty. A prompt file for a weather agent would be
an invitation to let a model summarise a wave height, which is exactly the
failure the whole architecture is arranged to prevent. The empty files are kept
as a marker, not as a to-do.

---------------------------------------------------------------------------
WHAT A DOMAIN AGENT IS FOR, GIVEN THAT THE EXECUTOR ALREADY RUNS TOOLS
---------------------------------------------------------------------------

It would be easy to write four classes that forward a call to a tool and add
nothing. That is the trap, and it is why these files sat empty. An agent earns
its place by owning the domain knowledge that lives *between* tools and does
not belong inside any one of them:

1.  **Which tools this domain owns.** Derived from the registry's ``agent``
    field, never hand-listed, for the same reason the planner's tool block is
    generated: a hand-maintained list drifts within a week.

2.  **What this domain contributes to a plan for a given intent.** "A safety
    question needs waves, wind, tides and an alert check" is weather knowledge.
    It currently lives in one monolithic ``FALLBACK_PLANS`` dict in
    ``orchestrator/validate_plan.py``, where the weather, ocean and geospatial
    slices of four different plans are interleaved. :meth:`Agent.plan_steps`
    lets each domain own its own slice.

3.  **A typed view of what its tools found.** From CLAUDE.md: *agents return
    typed fragments, never prose.* :class:`DomainFragment` is that fragment.
    It carries the domain's own degradation policy -- weather knows that a
    failed alert check is safety-critical while a failed tide read is not, and
    that judgement belongs in the weather agent rather than in a generic
    executor that treats every failure alike.

The executor stays responsible for *running* the DAG. Agents are responsible
for knowing what should be in it and what came back.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.intent import Intent, QueryType
from core.schemas.tool_io import ToolStatus
from tools import registry

if TYPE_CHECKING:  # pragma: no cover -- import cycle avoidance only
    from orchestrator.executor import ExecutionResult

__all__ = [
    "Agent",
    "AgentRequest",
    "DomainFragment",
    "FragmentFinding",
    "AGENTS",
    "agent_for",
    "all_agents",
    "fragments_for",
]


class FragmentFinding(BaseModel):
    """One typed thing a domain observed. Slots, never a finished sentence.

    CLAUDE.md: *claims are slot templates, not finished prose.* A finding holds
    the machine-readable pieces and a template key; turning that into English
    -- or Tamil -- happens in ``language/``, after verification, never here.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="Stable machine code, e.g. 'alert_check_failed'.")
    template: str = Field(description="Template id for language/templates.")
    slots: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = Field(default=None, description="Which plan step produced this.")
    tool_call_id: str | None = Field(
        default=None,
        description=(
            "The call this finding rests on. Carried so a claim built from it "
            "can cite evidence the verifier can walk back to the tool log."
        ),
    )
    safety_critical: bool = False


class DomainFragment(BaseModel):
    """What one agent contributes to a recommendation.

    Deliberately not a partial ``Recommendation``: a fragment has no verdict,
    no headline and no confidence. Assembling those from fragments is the
    synthesis agent's job, and keeping the boundary sharp is what stops four
    agents each inventing their own opinion of how safe the day is.
    """

    model_config = ConfigDict(extra="forbid")

    agent: str
    status: ToolStatus = ToolStatus.OK
    findings: list[FragmentFinding] = Field(default_factory=list)
    step_ids: list[str] = Field(default_factory=list)
    failed_steps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.status is not ToolStatus.OK

    @property
    def blocks_verdict(self) -> bool:
        """Whether this domain's failures must stop a clean verdict.

        True when a safety-critical finding failed. ``compute_risk_score``
        enforces the same rule from the other side; this is the domain-level
        statement of it, so a caller assembling fragments does not have to
        re-derive which failures matter.
        """
        return any(f.safety_critical for f in self.findings if f.key.endswith("_failed"))


class Agent(ABC):
    """A domain agent. Deterministic; no LLM anywhere in a subclass."""

    #: Matches ``tools.registry.AgentGroup``.
    group: str = ""
    #: Human label, for traces and the explainability drawer.
    label: str = ""

    @property
    def name(self) -> str:
        return type(self).__name__

    def tools(self, implemented_only: bool = True) -> list[str]:
        """Tool names this agent owns, from the registry rather than a list."""
        specs = registry.implemented_specs() if implemented_only else registry.all_specs()
        return [s.name for s in specs if s.agent == self.group]

    def owns(self, tool_name: str) -> bool:
        return registry.has(tool_name) and registry.get(tool_name).agent == self.group

    @abstractmethod
    def plan_steps(self, intent: Intent, base_id: int = 1) -> list[dict[str, Any]]:
        """This domain's contribution to a plan for ``intent``.

        Returns raw step dicts rather than ``PlanStep`` objects so the result
        can carry unresolved ``$PLACE`` placeholders, exactly as
        ``FALLBACK_PLANS`` does -- those are substituted before validation.

        ``base_id`` is the first step number to use, so several agents can be
        composed into one plan without colliding on ids. Returning an empty
        list means "this domain has nothing to add to this intent", which is
        the correct answer more often than not.
        """

    @abstractmethod
    def fragment(self, result: "ExecutionResult", intent: Intent) -> DomainFragment:
        """Read this domain's executed steps into a typed fragment."""

    def review(self, result: "ExecutionResult", intent: Intent) -> list["AgentRequest"]:
        """Inspect what ran and ask other agents for anything still needed.

        Called after each wave of execution. Returning an empty list -- the
        default -- means this domain is satisfied, which is the common case and
        the one that terminates the loop.

        Override this to express domain knowledge that only becomes relevant
        once data exists: *"I found a fishing zone 60 km out, and I do not know
        whether that water is legal"* is a question the ocean agent can only ask
        after seeing the candidate.

        Requests are advisory. Each becomes a plan step and goes through the
        same validation as any other, so this cannot be used to run something
        the validator would refuse.
        """
        return []

    # -- helpers shared by subclasses --------------------------------------

    def _my_records(self, result: "ExecutionResult") -> dict[str, Any]:
        """Step id -> tool call record, for the steps this agent owns.

        Ownership is resolved through the registry by tool name, not through
        the step's ``agent`` field. That field is advisory and may be absent or
        simply wrong on a model-produced plan; the registry cannot be.
        """
        return {
            record.step_id: record
            for record in result.tool_call_log.values()
            if record.step_id and self.owns(record.tool)
        }

    def _collect(
        self, result: "ExecutionResult", intent: Intent
    ) -> tuple[list[str], list[str], ToolStatus]:
        """Partition this agent's steps into ok and failed, and roll up status."""
        mine = sorted(self._my_records(result))
        failed = [
            step_id
            for step_id in mine
            if (out := result.outputs.get(step_id)) is None
            or out.status is ToolStatus.FAILED
        ]

        if mine and len(failed) == len(mine):
            status = ToolStatus.FAILED
        elif failed or any(
            (out := result.outputs.get(s)) is not None and out.status is ToolStatus.DEGRADED
            for s in mine
        ):
            status = ToolStatus.DEGRADED
        else:
            status = ToolStatus.OK
        return mine, failed, status

    def _finding(
        self,
        result: "ExecutionResult",
        step_id: str,
        key: str,
        template: str,
        slots: dict[str, Any],
        safety_critical: bool = False,
    ) -> FragmentFinding:
        return FragmentFinding(
            key=key,
            template=template,
            slots=slots,
            step_id=step_id,
            tool_call_id=result.call_id_for(step_id),
            safety_critical=safety_critical,
        )


#: Populated at import time by ``agents/__init__.py``. Keyed by group.
AGENTS: dict[str, Agent] = {}


def register_agent(agent: Agent) -> Agent:
    if agent.group in AGENTS:
        raise ValueError(f"Agent group {agent.group!r} is already registered.")
    AGENTS[agent.group] = agent
    return agent


def agent_for(group: str) -> Agent:
    if group not in AGENTS:
        raise KeyError(f"No agent for group {group!r}. Registered: {sorted(AGENTS)}")
    return AGENTS[group]


def all_agents() -> list[Agent]:
    return [AGENTS[k] for k in sorted(AGENTS)]


def fragments_for(result: "ExecutionResult", intent: Intent) -> list[DomainFragment]:
    """Every domain's view of one run, in a stable order.

    Agents with no steps in the run are skipped rather than returning an empty
    fragment, so a geofence query does not carry a hollow ocean block.
    """
    out: list[DomainFragment] = []
    for agent in all_agents():
        fragment = agent.fragment(result, intent)
        if fragment.step_ids or fragment.findings:
            out.append(fragment)
    return out


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

#: Symbolic step references an agent may emit when it needs another domain's
#: output but cannot know that domain's step numbering. ``$sWAVE`` means "the
#: step that ran wave_forecast", and :func:`compose_plan` resolves it once the
#: whole plan exists. The alternative -- agents agreeing on hardcoded step
#: numbers -- breaks the moment one of them plans a different number of steps.
STEP_ALIASES = {
    "$sWAVE": "wave_forecast",
    "$sWIND": "wind_forecast",
    "$sALERT": "active_alerts",
    "$sTIDE": "tides",
    "$sPLACE": "resolve_place",
}

#: Agents contribute in this order. Geospatial is first and not negotiable: it
#: owns the coordinate every other domain depends on, so it must hold s1.
COMPOSE_ORDER = [
    registry.AgentGroup.GEOSPATIAL,
    registry.AgentGroup.WEATHER,
    registry.AgentGroup.OCEAN,
    registry.AgentGroup.RISK,
]


def compose_plan(intent: Intent) -> dict[str, Any] | None:
    """Build a plan for ``intent`` by asking each domain what it needs.

    Returns a raw plan dict in the same shape ``FALLBACK_PLANS`` produces --
    still carrying ``$PLACE`` and ``$VESSEL_CLASS`` placeholders, which
    ``fallback_plan``-style substitution fills in before validation.

    Returns None when no agent contributed a step, which happens when the
    intent is missing the slots its domains need. That is a real outcome and
    the caller should treat it as "ask the user", not as an error.

    **This does not replace ``FALLBACK_PLANS``.** That dict is the hardcoded
    last resort and its whole value is being dumb and fixed. This is the
    composed path: same shape, assembled from the domains that own the
    knowledge, and validated by exactly the same ``validate_plan()``.
    """
    steps: list[dict[str, Any]] = []
    next_id = 1
    for group in COMPOSE_ORDER:
        agent = AGENTS.get(group)
        if agent is None:
            continue
        contributed = agent.plan_steps(intent, base_id=next_id)

        # Every other domain's steps depend on the coordinate the geospatial
        # agent resolves at s1. If it contributed nothing -- which it does when
        # the intent carries no place -- then nothing downstream can run, and
        # continuing would compose a plan whose steps all depend on an s1 that
        # does not exist. validate_plan() would reject that, but emitting a
        # structurally broken plan and relying on the validator to catch it is
        # not the same as not emitting one.
        if group == registry.AgentGroup.GEOSPATIAL and not contributed:
            return None

        steps.extend(contributed)
        next_id += len(contributed)

    if not steps:
        return None

    # Resolve symbolic references now that every step has a real id.
    by_tool = {step["tool"]: step["id"] for step in steps}

    def resolve(node: Any) -> Any:
        if isinstance(node, str):
            for alias, tool_name in STEP_ALIASES.items():
                if node.startswith(alias):
                    target = by_tool.get(tool_name)
                    if target is None:
                        return None
                    return f"${target}{node[len(alias):]}"
            return node
        if isinstance(node, dict):
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    resolved: list[dict[str, Any]] = []
    for step in steps:
        step = resolve(step)
        # An unresolvable alias means the step this one needed was never
        # planned. Drop the argument rather than emitting a null the tool's
        # schema would reject with a confusing message.
        step["args"] = {k: v for k, v in step["args"].items() if v is not None}
        step["depends_on"] = [
            d.lstrip("$") for d in step.get("depends_on", []) if d is not None
        ]
        resolved.append(step)

    return {
        "intent_type": intent.query_type.value,
        "is_fallback": False,
        "steps": resolved,
    }


# --------------------------------------------------------------------------
# Inter-agent collaboration
# --------------------------------------------------------------------------
#
# Until this existed, agents never spoke to each other. The composer asked each
# one "what steps do you need?" before anything ran, the executor ran the DAG,
# and the composer asked each one "what did you find?" afterwards. Data moved
# agent -> executor -> agent. Never agent -> agent. The plan was fixed the
# moment it was validated, which made "collaborative agents" a diagram rather
# than a behaviour.
#
# :meth:`Agent.review` closes that. After a wave of execution an agent inspects
# what came back and may ask ANOTHER agent, by name, to do something -- and the
# plan grows to include it.
#
# Two properties keep this honest rather than theatrical:
#
# 1.  **A request is conditional on runtime data, not on the query type.** The
#     ocean agent does not ask for a geofence check "because this is a PFZ
#     query". It asks because it found a candidate zone 60 km offshore and does
#     not itself know whether that water is legal to fish. Different data, same
#     query, different plan.
# 2.  **A request is a request, not a command.** It is turned into a plan step
#     and put through the *same* ``validate_plan()`` as everything else. An
#     agent cannot smuggle in a call that the validator would reject, and it
#     cannot mark its own request safety-critical to force it through.


@dataclass(frozen=True)
class AgentRequest:
    """One agent asking another to run something, and why.

    ``reason`` is written for a human and surfaces in the reasoning trace, so
    the explainability drawer can say *"the ocean agent asked the geospatial
    agent to check the boundary at the candidate zone"* rather than showing an
    extra step that appeared from nowhere.
    """

    from_agent: str
    to_agent: str
    tool: str
    args: dict[str, Any]
    reason: str
    #: Safety-critical requests are never dropped when the step budget is
    #: reached; convenience ones are.
    critical: bool = False

    @property
    def signature(self) -> tuple[str, str]:
        """Identity for de-duplication: same tool, same arguments."""
        return (self.tool, json.dumps(self.args, sort_keys=True, default=str))

    def describe(self) -> str:
        return f"{self.from_agent} -> {self.to_agent}: {self.tool} ({self.reason})"
