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

import concurrent.futures
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agents.intent_planner_agent import plan_query
from agents.narrate import narrate
from agents.suggest import SUGGEST_BUDGET_S, suggest_followups
from agents.synthesis_agent import build_recommendation
from core.schemas.intent import Intent
from core.schemas.recommendation import Recommendation
from language import SUPPORTED
from orchestrator.collaborate import run_with_collaboration
from orchestrator.progress import ProgressBus
from orchestrator.session import SESSIONS, Turn
from orchestrator.verifier import verify

__all__ = ["TurnResult", "run_turn"]

logger = logging.getLogger(__name__)

_COUNTER = {"n": 0}


def _next_turn_id() -> str:
    _COUNTER["n"] += 1
    return f"t_{datetime.now(timezone.utc):%Y%m%d}_{_COUNTER['n']:05d}"


@dataclass
class TurnResult:
    """Everything one turn produced, including how it got there."""

    turn_id: str
    state: str
    """answer | chat | clarification | refusal | error"""
    answer: str = ""
    recommendation: Recommendation | None = None
    intent: Intent | None = None

    # Set on a clarification, so a UI can render choices as buttons rather
    # than making the user retype "FRP boat".
    missing_slots: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    #: Where the answer-turn follow-ups came from: model | rules | static |
    #: mixed. Empty on turns that show no answer follow-ups.
    suggestion_source: str = ""

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


def _answer_to_clarification(query: str, pending) -> Intent | None:
    """Merge a reply into the intent the pending question was asked about.

    Returns None when the reply supplies nothing we asked for -- the user
    changed the subject, and it should be planned as a fresh question rather
    than forced into the old one.

    Extraction is deterministic: the gazetteer and the configured vessel
    classes, plus an exact match on an option value, because the UI renders
    those options as buttons and a click sends the value verbatim.
    """
    from agents import keyword_intent
    from core.schemas.intent import SpatialReference, VesselClass

    intent = pending.intent
    updates: dict[str, Any] = {}
    text = query.strip().lower()

    if intent.spatial_reference is None:
        place = keyword_intent.extract_place(query)
        if place:
            updates["spatial_reference"] = SpatialReference(name=place)

    if intent.vessel_class is None:
        vessel = keyword_intent.extract_vessel_class(query)
        if vessel is None:
            # An option button sends its value verbatim, e.g. "frp_9m".
            vessel = next((v.value for v in VesselClass if v.value == text), None)
        if vessel:
            updates["vessel_class"] = VesselClass(vessel)

    if not updates:
        return None

    filled = list(updates)
    return intent.model_copy(
        update={
            **updates,
            "raw_query": query,
            # Recorded so synthesis surfaces them as stated assumptions. The
            # user supplied these, but not in this turn's words.
            "inherited_slots": sorted(set(intent.inherited_slots) | set(filled)),
            "missing_slots": [s for s in intent.missing_slots if s not in filled],
        }
    )


def _plan_from_intent(intent: Intent) -> "PlanningResult":
    """Plan a fully-formed intent without asking the model again.

    The query type is already known -- it came from the question we asked --
    so the planner has nothing left to interpret. Going back to it would spend
    a call to be told what we already established, and risk it reclassifying a
    one-word reply.

    The slot gate still runs: if the reply filled only one of two missing
    slots, this returns a clarification for the rest rather than guessing.
    """
    from agents.intent_planner_agent import PlanningResult, _force_clarification, _use_fallback

    # The same region gate the model path runs. This is the path that exposed
    # its absence: a place supplied as an answer to a clarification reached the
    # executor without anyone checking it was in the box.
    from agents.intent_planner_agent import outside_box, refuse_out_of_region

    if intent.spatial_reference is not None:
        away = outside_box(intent.spatial_reference.name)
        if away is not None:
            return PlanningResult(
                output=refuse_out_of_region(intent, *away),
                attempts=0,
                notes=[f"{intent.spatial_reference.name} is outside the study area"],
            )

    gaps = intent.blocking_gaps()
    if gaps:
        return PlanningResult(
            output=_force_clarification(intent, gaps),
            attempts=0,
            notes=[f"answered from a clarification; still missing {gaps}"],
        )
    return _use_fallback(
        intent, ["answered from a clarification; slots complete"], 0, "session", "",
        deterministic=True,
    )


def run_turn(
    query: str,
    session_id: str | None = None,
    language: str = "en",
    progress: ProgressBus | None = None,
) -> TurnResult:
    """Answer one question. Never raises.

    A traceback mid-demo is worse than a stiff sentence, so every failure mode
    returns a ``TurnResult`` with a state the caller can render. That is the
    same policy the LLM client applies to providers, for the same reason.
    """
    started = datetime.now(timezone.utc)
    turn_id = _next_turn_id()

    # A reply to a question we asked is not a new question.
    #
    # When the previous turn was a clarification, this turn is the answer to it
    # and carries the slot we were missing. Merging it here -- before the
    # planner sees a bare "mechanised_trawler" with no context -- is what stops
    # the loop where the same question is asked forever.
    pending = SESSIONS.pending_question(session_id)
    merged = _answer_to_clarification(query, pending) if pending else None

    if merged is not None:
        planning = _plan_from_intent(merged)
    else:
        planning = plan_query(
            query,
            context=SESSIONS.context_for(session_id),
            history=SESSIONS.recent(session_id),
            dialogue=SESSIONS.conversation(session_id),
            awaiting=pending.answer if pending else None,
        )
    intent = planning.output.intent
    notes = list(planning.notes)

    def emit(stage: str, **detail: object) -> None:
        # Validation observably happened iff a plan exists (Gate 2 runs
        # inside plan_query), so it rides on the plan event rather than as
        # its own -- a separate validate event from here would be invented.
        if progress is not None:
            progress.emit(stage, **detail)

    emit(
        "plan",
        query_type=getattr(getattr(intent, "query_type", None), "value", "unknown"),
        fallback=bool(planning.used_fallback),
    )

    def finish(result: TurnResult) -> TurnResult:
        result.duration_ms = int(
            (datetime.now(timezone.utc) - started).total_seconds() * 1000
        )
        # One line per turn: how it was planned and narrated, for measuring
        # fallback frequency from logs. Counts only, no user text -- notes
        # travel on the result itself, not here.
        logger.info(
            "turn=%s state=%s provider=%s attempts=%d fallback=%s narration=%s suggest=%s notes=%d",
            result.turn_id,
            result.state,
            result.llm_provider,
            planning.attempts,
            result.used_fallback_plan,
            result.narration_source or "-",
            result.suggestion_source or "-",
            len(result.notes),
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
                missing_slots=result.missing_slots,
                options=result.options,
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

    # -- the planner just talked -------------------------------------------
    #
    # No tools ran, so there is nothing to verify and nothing to narrate. The
    # suggestions ride in `options`, which the UI already renders as buttons.
    if planning.state == "chat":
        reply = planning.output.chat
        return finish(
            TurnResult(
                state="chat",
                answer=reply.text if reply else "What would you like to know?",
                options=list(reply.suggestions) if reply else [],
                **base,
            )
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
        emit("execute", round=1)
        teamwork = run_with_collaboration(planning.plan, intent, turn_id=turn_id, progress=progress)
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
        emit("synthesise")
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
                + [
                    Caveat(text=concern)
                    for concern in teamwork.concerns
                    if concern and concern.strip()
                ]
            }
        )

    # -- verify, BEFORE any prose is produced ------------------------------
    report = verify(recommendation, execution.tool_call_log)
    emit("verify", ok=bool(report.ok), numbers_checked=int(report.numbers_checked))
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

    # -- narrate + suggest -------------------------------------------------
    # Only the narration source is emitted, never the text: streaming
    # partial prose would display numbers before the guard passes on the
    # complete text.
    #
    # Suggestions run CONCURRENTLY with narration in a ThreadPoolExecutor
    # (turn.py is fully synchronous -- no async runtime exists here).
    # Wait semantic: the answer ships at max(narration_done,
    # min(suggest_done, 6s)). Narration is never delayed by this feature;
    # a suggest call still in flight 6s past narration is abandoned and
    # the rule-based set ships instead. Note the abandon is
    # latency-bounded, not cost-free: the dropped future's HTTP request
    # may still complete server-side and spend tokens; what is bounded is
    # the user's wait. Both read the same already-verified object, so
    # there is no ordering dependency between them.
    prior_options = SESSIONS.prior_answer_options(session_id)
    # The allowlist is the adapter's own SUPPORTED tuple, not a copy of it:
    # a language with a templates/ directory can be answered, and one without
    # falls back WITH a recorded note. Silent fallback would read as a Tamil
    # answer that never came, and no fallback at all raises
    # NotImplementedError out of narrate via language.render.
    effective_language = language if language in SUPPORTED else "en"
    if effective_language != language:
        notes.append(f"language {language!r} not supported yet; answered in English")
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        narration_future = pool.submit(narrate, recommendation, effective_language)
        suggest_future = pool.submit(
            suggest_followups, recommendation, intent, prior_options
        )
        narration = narration_future.result()
        emit("narrate", source=narration.source)
        try:
            suggestions = suggest_future.result(timeout=SUGGEST_BUDGET_S)
        except concurrent.futures.TimeoutError:
            suggestions = None
            notes.append(
                "suggest call exceeded the 6s budget; rule-based follow-ups served"
            )
    finally:
        # Never wait for an abandoned suggest call here: the context
        # manager's implicit shutdown(wait=True) would hold the answer
        # for the full hung call, voiding the budget above.
        pool.shutdown(wait=False, cancel_futures=True)
    if suggestions is None:
        from agents.intent_planner_agent import SUGGESTIONS as _STATIC
        from agents.suggest import _assemble, apply_hygiene, rule_suggestions

        rules = [
            t
            for t in apply_hygiene(rule_suggestions(intent))
            if t not in prior_options
        ]
        static = [
            t for t in apply_hygiene(list(_STATIC)) if t not in prior_options
        ]
        suggestions = _assemble([(rules, "rules"), (static, "static")])
    return finish(
        TurnResult(
            state="answer",
            answer=narration.text,
            recommendation=recommendation,
            options=list(suggestions.texts),
            suggestion_source=suggestions.source,
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
