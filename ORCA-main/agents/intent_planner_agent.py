"""Intent + planner: one LLM call, two blocks, then deterministic gates.

The single place the LLM decides *what to do*. It never decides what is true.
From CLAUDE.md: the planner emits a step calling ``compute_risk_score()``; it
does not weigh a 2.8 m swell against a vessel class. Planning is not
adjudication.

The shape of this module is a funnel of code around one model call:

1.  **Prompt assembly.** The tool catalogue is injected from
    ``tools.registry.planner_tool_block()`` -- never hand-maintained, so the
    prompt cannot drift from the code. Only *implemented* tools are listed, so
    the model cannot plan a call we cannot run.
2.  **The call.** ``llm.complete("planner", ...)``. Never raises.
3.  **Parse** into :class:`PlannerOutput`, which enforces exactly-one-state.
4.  **Inheritance.** Missing slots are filled from the previous turn and
    recorded in ``inherited_slots``, so they surface as assumptions later.
5.  **The governing-rule gate.** A safety question with a missing location or
    vessel class becomes a clarification *in code*, regardless of what the
    model returned. The prompt asks the model to do this; this gate makes
    sure.
6.  **``validate_plan()``.** One replan with the errors fed back, then the
    hardcoded fallback for that intent type, then -- only if even that cannot
    run -- an honest refusal. The demo never reaches a state where a question
    has no answer at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.schemas.intent import (
    ClarificationRequest,
    Intent,
    PlannerOutput,
    QueryType,
    Refusal,
    RefusalReason,
    SpatialReference,
    VesselClass,
)
from core.schemas.plan import Plan
from orchestrator.llm import client as llm
from agents import keyword_intent
from orchestrator.validate_plan import PlanValidationResult, fallback_plan, validate_plan
from tools import registry

__all__ = ["PlanningResult", "plan_query", "parse_planner_json", "apply_inheritance"]

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "planner.md"

#: Slots a safety question may never proceed without. Mirrors
#: core.schemas.intent.SAFETY_CRITICAL_SLOTS; imported by name there and
#: asserted equal in tests so the two cannot drift.
_CLARIFICATION_QUESTIONS = {
    ("spatial_reference", "vessel_class"): "Which landing centre are you leaving from, and what kind of boat is it?",
    ("spatial_reference",): "Which landing centre are you leaving from?",
    ("vessel_class",): "What kind of boat is it?",
}


@dataclass
class PlanningResult:
    """What planning produced, and how it got there."""

    output: PlannerOutput
    plan: Plan | None = None
    attempts: int = 0
    used_fallback: bool = False
    llm_provider: str = "none"
    llm_model: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        return self.output.state


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------


def _system_prompt() -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return template.replace("{TOOLS}", registry.planner_tool_block()).replace(
        "{TODAY}", date.today().isoformat()
    )


def _user_message(query: str, context: Intent | None) -> str:
    if context is None:
        return query
    carried = {
        "previous_turn": {
            "query_type": context.query_type.value,
            "spatial_reference": context.spatial_reference.model_dump(exclude_none=True)
            if context.spatial_reference
            else None,
            "vessel_class": context.vessel_class.value if context.vessel_class else None,
        }
    }
    return f"{query}\n\nContext: {json.dumps(carried)}"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_planner_json(text: str, raw_query: str) -> PlannerOutput:
    """Turn the model's text into a validated :class:`PlannerOutput`.

    Tolerates code fences and leading prose, because models add them even
    when told not to. Does not tolerate a missing state or two populated
    blocks -- ``PlannerOutput`` rejects those, and that rejection is the point.
    """
    cleaned = _FENCE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in planner response")
    data: dict[str, Any] = json.loads(cleaned[start : end + 1])

    intent = data.get("intent") or {}
    intent.setdefault("raw_query", raw_query)
    intent["raw_query"] = raw_query  # ours, never the model's paraphrase
    data["intent"] = intent

    # Drop null blocks so exactly-one-state validation sees only what is set.
    for key in ("plan", "clarification", "refusal"):
        if data.get(key) is None:
            data.pop(key, None)

    return PlannerOutput.model_validate(data)


# --------------------------------------------------------------------------
# Deterministic gates
# --------------------------------------------------------------------------


def _evidenced_in(query: str, value: str, terms: tuple[str, ...] = ()) -> bool:
    """Whether this turn's own words support a slot value.

    Used to tell "the user said Nagapattinam again" apart from "the model
    carried Nagapattinam over from last turn". Both arrive as a filled slot;
    only the second is an assumption the user must be told about.
    """
    text = query.lower()
    if value.lower().replace("_", " ") in text:
        return True
    return any(term in text for term in terms)


def apply_inheritance(intent: Intent, context: Intent | None) -> Intent:
    """Fill missing slots from the previous turn, and say so.

    Every inherited slot is recorded in ``inherited_slots`` so the
    recommendation can list it under assumptions -- the user sees what was
    carried over on their behalf. Silent reuse is how a safety answer ends up
    describing the wrong boat.

    **Two ways a slot gets carried, and both must be recorded.** This function
    originally handled only the first:

    1.  The model left the slot ``None`` and we filled it here.
    2.  The model saw the previous turn in its prompt and **copied the slot
        itself**. Nothing was left to fill, so nothing was recorded, and the
        answer to "what about the day after?" silently described a boat and a
        port the user had not mentioned in that turn. Found on the first live
        multi-turn session, 2026-09-06.

    Case 2 is detected by asking whether this turn's own words support the
    value. If they do not, and the previous turn's do, it was inherited --
    whoever actually wrote it in.
    """
    if context is None:
        return intent

    from agents.keyword_intent import _VESSEL_TERMS

    updates: dict[str, Any] = {}
    inherited = list(intent.inherited_slots)
    query = intent.raw_query or ""

    if intent.spatial_reference is None and context.spatial_reference is not None:
        updates["spatial_reference"] = context.spatial_reference
        inherited.append("spatial_reference")
    elif (
        intent.spatial_reference is not None
        and context.spatial_reference is not None
        and "spatial_reference" not in inherited
        and intent.spatial_reference.name == context.spatial_reference.name
        and not _evidenced_in(query, intent.spatial_reference.name or "")
    ):
        inherited.append("spatial_reference")

    if intent.vessel_class is None and context.vessel_class is not None:
        updates["vessel_class"] = context.vessel_class
        inherited.append("vessel_class")
    elif (
        intent.vessel_class is not None
        and context.vessel_class is not None
        and "vessel_class" not in inherited
        and intent.vessel_class == context.vessel_class
        and not _evidenced_in(
            query,
            intent.vessel_class.value,
            _VESSEL_TERMS.get(intent.vessel_class.value, ()),
        )
    ):
        inherited.append("vessel_class")

    if not updates and inherited == list(intent.inherited_slots):
        return intent
    updates["inherited_slots"] = inherited
    updates["missing_slots"] = [s for s in intent.missing_slots if s not in inherited]
    return intent.model_copy(update=updates)


def _force_clarification(intent: Intent, gaps: list[str]) -> PlannerOutput:
    """The governing rule, applied in code: never guess for a safety question."""
    key = tuple(gaps)
    question = _CLARIFICATION_QUESTIONS.get(key) or _CLARIFICATION_QUESTIONS[
        ("spatial_reference", "vessel_class")
    ]
    options = [v.value for v in VesselClass] if "vessel_class" in gaps else []
    return PlannerOutput(
        intent=intent.model_copy(update={"missing_slots": gaps}),
        state="clarification",
        clarification=ClarificationRequest(
            missing_slots=gaps,
            question_template=question,
            options=options,
            partial_intent=intent,
        ),
    )


def _refuse(intent: Intent, reason: RefusalReason, why: str) -> PlannerOutput:
    return PlannerOutput(
        intent=intent,
        state="refusal",
        refusal=Refusal(
            reason=reason,
            explanation_template=why,
            supported_region="South Coromandel: Chennai to Rameswaram, 78.5-82.0 E, 8.0-12.0 N",
        ),
    )


def _fallback_substitutions(intent: Intent) -> dict[str, Any] | None:
    place = intent.spatial_reference.name if intent.spatial_reference else None
    if not place:
        return None
    subs: dict[str, Any] = {"PLACE": place}
    if intent.vessel_class is not None:
        subs["VESSEL_CLASS"] = intent.vessel_class.value
    elif intent.query_type is QueryType.SAFETY_ASSESS:
        return None
    else:
        subs["VESSEL_CLASS"] = VesselClass.FRP_9M.value  # non-safety: harmless default
    return subs


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def plan_query(query: str, context: Intent | None = None) -> PlanningResult:
    """Plan a user query. Returns exactly one of plan / clarification / refusal.

    Never raises on model failure. If the model is unreachable, the hardcoded
    fallback plan is used when the intent can be inferred from context; if
    it cannot, the result is an honest clarification.
    """
    notes: list[str] = []

    # Gate 0: a greeting is not a follow-up.
    #
    # Found live on 2026-09-06: after a fishing-zone question, typing "hello"
    # returned a full fishing-zone answer. Context inheritance was working as
    # designed and the model, given a previous turn and nothing else, simply
    # continued it. Inheritance is meant to fill a *missing slot*, never to
    # manufacture a request the user did not make.
    #
    # The gate is deliberately narrow: it fires only when the entire utterance
    # is a pleasantry. "What about the day after?" carries no marine keyword
    # either and is a genuine follow-up, so a "must mention the sea" rule would
    # break multi-turn conversation, which is a stated requirement.
    if keyword_intent.is_conversational_filler(query):
        intent = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query=query)
        return PlanningResult(
            output=_force_clarification(intent, ["spatial_reference", "vessel_class"]),
            attempts=0,
            notes=["greeting with no request; context deliberately not inherited"],
        )

    system = _system_prompt()
    user = _user_message(query, context)

    first = llm.complete("planner", system, user)
    attempts = 1

    if not first.ok:
        notes.append(f"planner LLM unavailable: {first.error}")
        return _plan_without_llm(query, context, notes, attempts)

    try:
        output = parse_planner_json(first.text, query)
    except (ValueError, ValidationError, json.JSONDecodeError) as exc:
        notes.append(f"planner response unparseable: {exc}")
        # One retry with the parse error fed back, then give up on the model.
        retry = llm.complete(
            "planner",
            system,
            f"{user}\n\nYour previous response was rejected: {exc}. Return only the JSON object.",
        )
        attempts += 1
        if not retry.ok:
            return _plan_without_llm(query, context, notes, attempts)
        try:
            output = parse_planner_json(retry.text, query)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc2:
            notes.append(f"replan also unparseable: {exc2}")
            return _plan_without_llm(query, context, notes, attempts)

    return _finish(output, query, context, notes, attempts, system, user, first)


def _finish(
    output: PlannerOutput,
    query: str,
    context: Intent | None,
    notes: list[str],
    attempts: int,
    system: str,
    user: str,
    first: llm.LLMResult,
) -> PlanningResult:
    intent = apply_inheritance(output.intent, context)
    output = output.model_copy(update={"intent": intent})

    # Gate 1: the governing rule. Code decides this, not the model.
    gaps = intent.blocking_gaps()
    if gaps:
        if output.state != "clarification":
            notes.append(f"model returned '{output.state}' with missing {gaps}; forced clarification")
        return PlanningResult(
            output=_force_clarification(intent, gaps),
            attempts=attempts,
            llm_provider=first.provider,
            llm_model=first.model,
            notes=notes,
        )

    # Gate 1b: the model may not redefine scope. Seen live on 2026-09-04: asked
    # a pfz_locate question, the model found no pfz tool in the catalogue
    # (unimplemented tools are not advertised) and refused as out_of_scope,
    # telling the user this system does not locate fishing zones. It does;
    # the data is not connected yet. Those are different sentences, and only
    # one of them is true. The four query types are always in scope; when
    # the model refuses one of them, the reason is ours.
    if output.state == "refusal" and output.refusal is not None:
        if output.refusal.reason is RefusalReason.OUT_OF_SCOPE:
            notes.append(
                f"model refused {intent.query_type.value} as out_of_scope; overridden -- "
                "the four query types are always in scope"
            )
            output = _refuse(
                intent,
                RefusalReason.NO_DATA,
                "This kind of question cannot be answered yet: the data it needs is not connected.",
            )

    if output.state in ("clarification", "refusal"):
        return PlanningResult(
            output=output, attempts=attempts, llm_provider=first.provider, llm_model=first.model, notes=notes
        )

    # Gate 2: validate_plan, one replan, then fallback.
    result = validate_plan(output.plan)
    if not result.ok:
        notes.append(f"plan rejected: {result.errors}")
        retry = llm.complete("planner", system, f"{user}\n\n{result.error_feedback()}")
        attempts += 1
        if retry.ok:
            try:
                retried = parse_planner_json(retry.text, query)
                if retried.state == "plan":
                    result = validate_plan(retried.plan)
                    if result.ok:
                        output = retried.model_copy(update={"intent": intent})
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                notes.append(f"replan unparseable: {exc}")

    if result.ok:
        return PlanningResult(
            output=output,
            plan=result.plan,
            attempts=attempts,
            llm_provider=first.provider,
            llm_model=first.model,
            notes=notes,
        )

    notes.append("replan failed; using hardcoded fallback plan")
    return _use_fallback(intent, notes, attempts, first.provider, first.model)


def _plan_without_llm(
    query: str, context: Intent | None, notes: list[str], attempts: int
) -> PlanningResult:
    """No usable model. Lean on context, then on keywords, then ask.

    Three tiers, in decreasing confidence:

    1.  **The previous turn.** Same query type, same slots, every carried slot
        recorded in ``inherited_slots`` so it surfaces as an assumption.
    2.  **Keyword classification** (``agents/keyword_intent.py``). Proposes a
        query type only -- never a coordinate, never a vessel class -- so the
        slot gates still fire and a safety question missing its place is still
        asked back. Added 2026-09-06: without it, a deployment with no key and
        no Ollama could not classify anything, and every question over the API
        became "which boat, and from where?" regardless of what was asked.
    3.  **A clarification.** The honest answer when nothing else applies.

    Tier 2 is a substitute for the model, not a shortcut past it. It is reached
    only after every provider has failed.
    """
    if context is None:
        guess = keyword_intent.classify(query)
        if guess is not None:
            # Slots are matched against the gazetteer and the configured vessel
            # classes -- a lookup, not an interpretation. Anything not found is
            # left empty so the slot gate asks for it.
            place = keyword_intent.extract_place(query)
            vessel = keyword_intent.extract_vessel_class(query)
            found = [
                name
                for name, value in (("spatial_reference", place), ("vessel_class", vessel))
                if value
            ]
            intent = Intent(
                query_type=guess.query_type,
                raw_query=query,
                spatial_reference=SpatialReference(name=place) if place else None,
                vessel_class=VesselClass(vessel) if vessel else None,
                # Recorded as inherited so synthesis surfaces them as stated
                # assumptions. A keyword match cannot read negation, and the
                # mitigation is that the user is told what was assumed.
                inherited_slots=found,
            )
            notes = notes + [
                f"no LLM; query type {guess.query_type.value} inferred from "
                f"keywords {guess.matched} (score {guess.score})"
                + (f"; slots matched deterministically: {found}" if found else "")
            ]
            gaps = intent.blocking_gaps()
            if gaps:
                return PlanningResult(
                    output=_force_clarification(intent, gaps),
                    attempts=attempts,
                    notes=notes,
                )
            return _use_fallback(intent, notes, attempts, "keyword", "")

        intent = Intent(query_type=QueryType.SAFETY_ASSESS, raw_query=query)
        return PlanningResult(
            output=_force_clarification(intent, ["spatial_reference", "vessel_class"]),
            attempts=attempts,
            notes=notes + ["no LLM, no context, and keywords were inconclusive"],
        )
    intent = context.model_copy(update={"raw_query": query, "inherited_slots": ["spatial_reference", "vessel_class"]})
    gaps = intent.blocking_gaps()
    if gaps:
        return PlanningResult(output=_force_clarification(intent, gaps), attempts=attempts, notes=notes)
    return _use_fallback(intent, notes, attempts, "none", "")


def _use_fallback(
    intent: Intent, notes: list[str], attempts: int, provider: str, model: str
) -> PlanningResult:
    subs = _fallback_substitutions(intent)
    if subs is None:
        gaps = intent.blocking_gaps() or ["spatial_reference"]
        return PlanningResult(
            output=_force_clarification(intent, gaps), attempts=attempts, notes=notes
        )
    raw = fallback_plan(intent.query_type, subs)
    result: PlanValidationResult = validate_plan(raw)
    if result.ok:
        output = PlannerOutput(intent=intent, state="plan", plan=raw)
        return PlanningResult(
            output=output,
            plan=result.plan,
            attempts=attempts,
            used_fallback=True,
            llm_provider=provider,
            llm_model=model,
            notes=notes,
        )
    # Even the fallback cannot run -- typically because the tools this query
    # type needs are not implemented yet. Say so rather than pretend.
    notes.append(f"fallback plan invalid: {result.errors}")
    return PlanningResult(
        output=_refuse(
            intent,
            RefusalReason.NO_DATA,
            "This kind of question cannot be answered yet: the data it needs is not connected.",
        ),
        attempts=attempts,
        used_fallback=True,
        llm_provider=provider,
        llm_model=model,
        notes=notes,
    )
