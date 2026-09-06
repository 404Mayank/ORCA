"""One full turn: question in, verified answer out.

The whole pipeline in one function, so that the HTTP route, the CLI scripts and
any future channel all run **exactly the same path**. A demo that works over
HTTP but not from a script, or vice versa, is a demo with two behaviours and no
way to tell which one a judge saw.

    plan -> validate -> execute -> synthesise -> verify -> narrate

The two gates are non-negotiable and both live here rather than in the route:

* ``validate_plan()`` runs before any tool executes. The planner already calls
  it; this function never bypasses it.
* ``verify()`` runs before any prose is produced. **If verification fails the
  narration is not attempted at all** -- there is no path in this module where
  an unverified object reaches an LLM, because the narrator's guard only
  compares prose against the rendering, and a rendering of a bad object is
  faithfully rendered nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agents.intent_planner_agent import plan_query
from agents.narrate import narrate
from agents.synthesis_agent import build_recommendation
from core.schemas.intent import Intent
from core.schemas.recommendation import Recommendation
from orchestrator.collaborate import run_with_collaboration
from orchestrator.session import SESSIONS, Turn
from orchestrator.verifier import verify

__all__ = ["TurnResult", "run_turn"]

_COUNTER = {"n": 0}


def _next_turn_id() -> str:
    _COUNTER["n"] += 1
    return f"t_{datetime.now(timezone.utc):%Y%m%d}_{_COUNTER['n']:05d}"


@dataclass
class TurnResult:
    """Everything one turn produced, including how it got there."""

    turn_id: str
    state: str
    """answer | clarification | refusal | error"""
    answer: str = ""
    recommendation: Recommendation | None = None
    intent: Intent | None = None

    # Set on a clarification, so a UI can render choices as buttons rather
    # than making the user retype "FRP boat".
    missing_slots: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)

    #: Inter-agent requests accepted this turn, as human-readable lines.
    collaboration: list[str] = field(default_factory=list)
    collaboration_rounds: int = 0
    #: "AgentName: what it concluded", one per agent that reasoned this turn.
    #: This is the visible evidence that each domain agent thought for itself
    #: rather than following a rule someone typed.
    agent_reasoning: list[str] = field(default_factory=list)

    verified: bool | None = None
    verifier_errors: list[str] = field(default_factory=list)
    numbers_checked: int = 0

    narration_source: str = ""
    narration_fallback_reason: str | None = None

    llm_provider: str = "none"
    llm_model: str = ""
    used_fallback_plan: bool = False
    degraded: bool = False
    notes: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def verdict(self) -> str | None:
        if self.recommendation is None or self.recommendation.verdict is None:
            return None
        # str(), not .value: VerdictValue is a str enum, so returning it raw
        # satisfies pydantic on the API boundary while handing every other
        # consumer -- the session store, /session/{id}, logs -- the repr
        # "VerdictValue.GO" instead of "go".
        return str(self.recommendation.verdict.value.value)


def run_turn(
    query: str,
    session_id: str | None = None,
    language: str = "en",
) -> TurnResult:
    """Answer one question. Never raises.

    A traceback mid-demo is worse than a stiff sentence, so every failure mode
    returns a ``TurnResult`` with a state the caller can render. That is the
    same policy the LLM client applies to providers, for the same reason.
    """
    started = datetime.now(timezone.utc)
    turn_id = _next_turn_id()
    context = SESSIONS.context_for(session_id)

    planning = plan_query(query, context=context)
    intent = planning.output.intent
    notes = list(planning.notes)

    def finish(result: TurnResult) -> TurnResult:
        result.duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        SESSIONS.record(
            session_id,
            Turn(
                turn_id=result.turn_id,
                query=query,
                intent=result.intent,
                state=result.state,
                answer=result.answer,
                verdict=result.verdict,
            ),
        )
        return result

    base = dict(
        turn_id=turn_id,
        intent=intent,
        llm_provider=planning.llm_provider,
        llm_model=planning.llm_model,
        used_fallback_plan=planning.used_fallback,
        notes=notes,
    )

    # -- the planner asked, or refused -------------------------------------
    if planning.state == "clarification":
        clarification = planning.output.clarification
        # question_template holds a rendered question today; slots and options
        # travel beside it so a client can present choices rather than prose.
        return finish(
            TurnResult(
                state="clarification",
                answer=(
                    clarification.question_template
                    if clarification
                    else "Could you say a little more about where and which boat?"
                ),
                missing_slots=list(clarification.missing_slots) if clarification else [],
                options=[str(o) for o in (clarification.options or [])] if clarification else [],
                **base,
            )
        )

    if planning.state == "refusal" or planning.plan is None:
        refusal = planning.output.refusal
        return finish(
            TurnResult(
                state="refusal",
                answer=(
                    refusal.explanation_template
                    if refusal
                    else "This question cannot be answered yet."
                ),
                **base,
            )
        )

    # -- execute -----------------------------------------------------------
    try:
        # Not a bare execution: agents review what came back and may extend the
        # plan, then it runs again. See orchestrator/collaborate.py.
        teamwork = run_with_collaboration(planning.plan, intent, turn_id=turn_id)
        execution = teamwork.result
        notes.extend(teamwork.notes)
        notes.extend(f"dropped: {d}" for d in teamwork.dropped)
    except Exception as exc:  # noqa: BLE001 -- reported, never raised at a user
        notes.append(f"executor raised {type(exc).__name__}: {exc}")
        return finish(
            TurnResult(
                state="error",
                answer="Something failed while gathering the data for this answer.",
                **base,
            )
        )

    # -- synthesise --------------------------------------------------------
    try:
        recommendation = build_recommendation(execution, intent, turn_id=turn_id)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"synthesis raised {type(exc).__name__}: {exc}")
        return finish(
            TurnResult(
                state="error",
                answer="The data came back but the answer could not be assembled.",
                degraded=True,
                **base,
            )
        )

    # -- fold in what the agents said, before verification -----------------
    #
    # Concerns come from the domain agents' own reasoning and are added as
    # caveats. They are added BEFORE verify() runs, so the object the verifier
    # checks is the object the user sees -- appending afterwards would mean
    # showing text that never passed the gate.
    #
    # These are safe to add because they carry no numbers: strip_numbers()
    # removed them at the source. A caveat is exactly where an invented figure
    # would look most authoritative, which is why the guard is there and not
    # here.
    if teamwork.concerns:
        from core.schemas.recommendation import Caveat

        recommendation = recommendation.model_copy(
            update={
                "caveats": list(recommendation.caveats)
                + [Caveat(text=concern) for concern in teamwork.concerns]
            }
        )

    # -- verify, BEFORE any prose is produced ------------------------------
    report = verify(recommendation, execution.tool_call_log)
    if not report.ok:
        # Deliberately not narrated. Rendering an object that failed
        # verification would produce a fluent, confident, unsupported answer --
        # exactly what the verifier exists to stop.
        notes.append("verifier rejected the recommendation; narration skipped")
        return finish(
            TurnResult(
                state="error",
                answer=(
                    "This answer could not be verified against the data it was "
                    "built from, so it is not being shown."
                ),
                recommendation=recommendation,
                verified=False,
                verifier_errors=[str(e) for e in report.errors],
                numbers_checked=report.numbers_checked,
                degraded=True,
                **base,
            )
        )

    # -- narrate -----------------------------------------------------------
    narration = narrate(recommendation, language=language)
    return finish(
        TurnResult(
            state="answer",
            answer=narration.text,
            recommendation=recommendation,
            collaboration=teamwork.describe(),
            collaboration_rounds=teamwork.rounds,
            agent_reasoning=[
                f"{d.agent}: {d.assessment}"
                for d in teamwork.deliberations
                if d.used_llm and d.assessment
            ],
            verified=True,
            numbers_checked=report.numbers_checked,
            narration_source=narration.source,
            narration_fallback_reason=narration.fallback_reason,
            degraded=execution.degraded,
            **base,
        )
    )
