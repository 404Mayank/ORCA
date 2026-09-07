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
    ChatReply,
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
    return template.replace("{TOOLS}", registry.compact_tool_block(with_types=True)).replace(
        "{TODAY}", date.today().isoformat()
    )


def _user_message(
    query: str,
    context: Intent | None,
    history: list[tuple[str, str]] | None = None,
    conversational_only: bool = False,
    dialogue: list[dict[str, str]] | None = None,
) -> str:
    """The turn, plus what the conversation has established so far.

    The shape of this matters more than it looks. It used to hand the model a
    ``previous_turn`` object with ``query_type`` inside it, presented as neutral
    context -- and the model did the natural thing and carried the query type
    forward. Asked "why has the catch dropped there?" after a fishing-zone
    question, it returned another fishing-zone answer; three turns in a row
    answered a question nobody had asked.

    So the two kinds of context are now separated and labelled:

    * ``carry_these_slots`` -- place and vessel, which persist until changed.
    * ``recent_turns`` -- what was asked before, for resolving "there" and "the
      day after". Explicitly NOT a template for this turn's type.
    """
    if context is None and not history and not dialogue:
        return query

    payload: dict[str, Any] = {}
    if context is not None:
        payload["carry_these_slots"] = {
            "spatial_reference": context.spatial_reference.model_dump(exclude_none=True)
            if context.spatial_reference
            else None,
            "vessel_class": context.vessel_class.value if context.vessel_class else None,
        }
    if dialogue:
        payload["conversation"] = dialogue
    if history:
        payload["recent_turns"] = [
            {"asked": q, "was_classified_as": t} for q, t in history[-3:]
        ]
    if conversational_only:
        # A greeting shown alongside slots is an invitation to re-ask the last
        # question with them. Shown alongside the conversation only, it is an
        # invitation to talk -- which is what was actually wanted.
        payload["reminder"] = (
            "This turn is a greeting or small talk, not a request. Reply with "
            "state 'chat', in your own words, referring naturally to what has "
            "been discussed if there is anything to refer to. Do not re-answer "
            "an earlier question and do not assume a place or a boat."
        )
    else:
        payload["reminder"] = (
            "Classify THIS turn on its own words. Carry the slots; do not carry the "
            "query type. A new question about the same place is a new question."
        )
    return f"{query}\n\nConversation so far: {json.dumps(payload)}"


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

    # A chat turn is not one of the four query types, and the model says so by
    # leaving `query_type` null -- which is correct, and which `Intent` cannot
    # represent, because a required enum has no "none of these" member. Seen
    # live on 2026-09-07: the model returned a perfectly good chat reply, the
    # parse failed on the null, the retry failed the same way, and the turn
    # dropped to the keyword tier and answered "I did not catch what you need"
    # to a question it had understood fine.
    #
    # Filling a placeholder is safe **only** because nothing downstream reads
    # it: `_chat()` rebuilds the intent from scratch and `run_turn` returns
    # before planning. Do not widen this to any other state.
    if not intent.get("query_type"):
        if data.get("state") == "chat":
            intent["query_type"] = QueryType.SAFETY_ASSESS.value
        else:
            # The model asked for a slot without committing to a type. Seen on
            # "what is safest route for my vessel" (2026-09-07): a real safety
            # question, correctly recognised as needing a landing centre and a
            # boat, thrown away because `query_type` is a required enum and the
            # model had left it null. The turn came back as "I cannot reach my
            # language model", which was false -- the model had answered well.
            #
            # A clarification is going to ask for the missing slot either way,
            # so the type only has to be *safe*. Keywords first, then
            # safety_assess, which is the most cautious of the four: it is the
            # one that refuses to guess a place or a boat.
            guess = keyword_intent.classify(raw_query)
            intent["query_type"] = (
                guess.query_type.value if guess else QueryType.SAFETY_ASSESS.value
            )

    # Cosmetic limits must not destroy a good turn.
    #
    # `suggestions` is capped at four because four buttons fit. When the model
    # offered five, pydantic raised, the retry offered five again, and the turn
    # fell to the keyword tier -- which answered "I did not catch what you need"
    # to a question the model had answered well. Seen on three turns in one
    # session, 2026-09-07. A layout constraint is not a safety constraint and
    # must not fail like one: trim it and carry on.
    #
    # `strip_numbers` in _chat() is the constraint that still fails loudly,
    # because that one is about truth rather than about fitting on screen.
    chat_block = data.get("chat")
    if isinstance(chat_block, dict):
        raw_suggestions = chat_block.get("suggestions")
        if isinstance(raw_suggestions, list):
            chat_block["suggestions"] = [
                str(item) for item in raw_suggestions if str(item).strip()
            ][:4]

    intent.setdefault("raw_query", raw_query)
    intent["raw_query"] = raw_query  # ours, never the model's paraphrase
    data["intent"] = intent

    # Drop null blocks so exactly-one-state validation sees only what is set.
    for key in ("plan", "clarification", "refusal", "chat"):
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


#: What ORCA can do, for the tier where no model is reachable at all.
#:
#: This is a **fallback**, not the voice of the product. It was briefly the
#: primary answer to every greeting -- one fixed paragraph, identical each
#: time, chosen by a hardcoded word list -- and the result was that "alloo",
#: which the list did not contain, reached the model and got a better reply
#: than "hello" did. Conversation belongs to the model; this exists so the
#: system still says something sensible when every provider is down.
CAPABILITIES = (
    "I read live wave, wind, tide, satellite sea-surface-temperature and "
    "chlorophyll data for the coast between Chennai and Rameswaram, and I can "
    "tell you whether it is safe to go out, where the fish are likely to be, "
    "which waters to stay out of, and why a catch has fallen off. Every number "
    "I give you comes from a real measurement you can open and inspect."
)

SUGGESTIONS = [
    "Is it safe to go out from Nagapattinam tomorrow morning?",
    "Where is the nearest fishing zone from Rameswaram?",
    "Which zones must I avoid near Rameswaram?",
    "Why has my catch declined off Cuddalore?",
]


def _chat(query: str, text: str, suggestions: list[str] | None = None) -> PlannerOutput:
    """A conversational turn: talk back, call nothing.

    ``query_type`` still has to be something -- ``Intent`` requires one -- but
    nothing downstream reads it on this path, because ``run_turn`` returns
    before planning. It is set to the most common query so that a follow-up
    inheriting from this turn inherits something harmless.
    """
    from agents.deliberate import strip_numbers

    # The rule holds here too. A chat reply reaches no evidence block, so a
    # figure in it could never be verified -- and an unverifiable figure in
    # confident prose is precisely what the verifier exists to stop.
    safe = strip_numbers(text).strip()
    return PlannerOutput(
        intent=Intent(query_type=QueryType.SAFETY_ASSESS, raw_query=query),
        state="chat",
        chat=ChatReply(
            text=safe or CAPABILITIES,
            suggestions=list(suggestions or SUGGESTIONS)[:4],
        ),
    )


ROUTER_PATH = Path(__file__).resolve().parent / "prompts" / "router.md"


def _route(
    query: str,
    dialogue: list[dict[str, str]] | None,
    awaiting: str | None = None,
) -> PlanningResult | None:
    """Decide whether this turn needs the planner at all.

    **This exists because of arithmetic, not architecture.** The planner system
    prompt is over three thousand tokens -- it carries the tool catalogue, the
    slot rules and the plan grammar -- and a Groq free-tier key allows eight
    thousand tokens per minute. One planner call therefore consumes most of a
    minute's budget, and a second one inside the same minute is refused. That is
    exactly what happened live on 2026-09-07: a greeting, a real question and a
    follow-up in quick succession, and the last two came back as "I cannot reach
    my language model" while the key was perfectly healthy.

    Sending fourteen tool schemas to decide whether "hello" is a greeting was
    always waste. This call asks one small question with a small prompt, and
    only a turn that is genuinely about the sea goes on to pay for the planner.

    Returns a finished chat result, or None meaning "this is a real query, plan
    it properly". A router failure also returns None: when in doubt the planner
    runs, because losing a safety question is far worse than spending tokens.
    """
    system = ROUTER_PATH.read_text(encoding="utf-8")
    payload: dict[str, Any] = {"user_said": query}
    if dialogue:
        payload["conversation"] = dialogue
    if awaiting:
        # Buried in the conversation list this was too easy to miss: asked
        # "which landing centre?", the user said "tell me", and the reply was
        # cheerful small talk that quietly dropped the question. Stated as its
        # own field it is hard to overlook.
        payload["you_are_still_waiting_for_an_answer_to"] = awaiting

    result = llm.complete("deliberator", system, json.dumps(payload))
    if not result.ok:
        return None

    try:
        cleaned = _FENCE.sub("", result.text.strip())
        data = json.loads(cleaned[cleaned.find("{") : cleaned.rfind("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return None

    if str(data.get("kind", "")).lower() != "chat":
        return None

    text = str(data.get("text", "")).strip()
    if not text:
        return None
    suggestions = [str(x) for x in (data.get("suggestions") or []) if str(x).strip()]
    return PlanningResult(
        output=_chat(query, text, suggestions),
        attempts=1,
        llm_provider=result.provider,
        llm_model=result.model,
        notes=["routed as conversation; planner not called"],
    )


_CHAT_SYSTEM = (
    "You are ORCA, a marine advisory assistant for fishermen on the Tamil Nadu "
    "coast between Chennai and Rameswaram. You can assess whether it is safe to "
    "go out, locate likely fishing zones, flag waters to avoid, and explain why "
    "a catch has fallen off. Reply to the user in one or two short, warm "
    "sentences, in your own words. "
    "You have called no tool, so you know nothing numeric. Write NO digits at "
    "all -- no wave heights, distances, dates or coordinates. Any you write are "
    "stripped before the user sees them, which will make your sentence read as "
    "broken. "
    'Return only JSON: {"text": "...", "suggestions": ["...", "..."]} with at '
    "most four suggestions -- real questions this user could ask next, drawn "
    "from what they have already said where possible."
)


def _chat_via_llm(
    query: str, dialogue: list[dict[str, str]] | None, notes: list[str]
) -> PlanningResult | None:
    """Ask the model to simply talk, with no plan schema to satisfy.

    Reached when the planner's structured output could not be parsed. The
    model is demonstrably up; only the shape it replied in was unusable. This
    call removes the shape, so a turn the model understood perfectly well is
    answered by the model rather than by a fixed paragraph.

    Returns None if this call also fails, in which case the caller drops to the
    genuinely model-free tier.
    """
    payload: dict[str, Any] = {"user_said": query}
    if dialogue:
        payload["conversation"] = dialogue
    result = llm.complete("deliberator", _CHAT_SYSTEM, json.dumps(payload))
    if not result.ok:
        notes.append(f"conversational retry also failed: {result.error}")
        return None
    try:
        cleaned = _FENCE.sub("", result.text.strip())
        data = json.loads(cleaned[cleaned.find("{") : cleaned.rfind("}") + 1])
        text = str(data.get("text", "")).strip()
        suggestions = [str(x) for x in (data.get("suggestions") or []) if str(x).strip()]
    except (ValueError, json.JSONDecodeError):
        # Not JSON, but it is prose, and prose is all this call was for.
        text, suggestions = result.text.strip(), []
    if not text:
        return None
    notes.append("planner schema failed; answered conversationally by the model")
    return PlanningResult(
        output=_chat(query, text, suggestions),
        attempts=1,
        llm_provider=result.provider,
        llm_model=result.model,
        notes=notes,
    )


def outside_box(place: str | None) -> tuple[float, float] | None:
    """Coordinates of a *known* place that lies outside the study area, else None.

    **The region test belongs in code, and until 2026-09-07 it existed only in
    the planner prompt.** The consequence showed up the moment a path skipped
    the model: asked for conditions and answered "Chennai" to the follow-up,
    the clarification path called ``_use_fallback`` directly, no LLM saw the
    turn, nothing checked the latitude, and the run produced "waves 0-0 m,
    wind 0-0 kn" from grid cells that do not exist. The verifier caught it and
    refused to show the answer, which is the safety net working -- but a
    refusal at the end of a wasted turn is not the same as declining at the
    start of one.

    Chennai sits at 13.08 N; the box stops at 12.0. ``config/bbox.yaml`` labels
    it exactly that way -- ``note: "north of box, refusal test"`` -- so the
    case was anticipated and simply never implemented.

    Returns None for an unknown place: the gazetteer cannot adjudicate a name
    it has never seen, and ``resolve_place`` will ask about it downstream.
    """
    if not place:
        return None
    from core import config

    box = config.load_yaml("bbox.yaml").get("bbox", {})
    points = config.load_yaml("bbox.yaml").get("reference_points", {})
    key = place.strip().lower().replace(" ", "_")
    point = points.get(key)
    if not point:
        return None
    lat, lon = float(point["lat"]), float(point["lon"])
    inside = (
        float(box.get("lat_min", 8.0)) <= lat <= float(box.get("lat_max", 12.0))
        and float(box.get("lon_min", 78.5)) <= lon <= float(box.get("lon_max", 82.0))
    )
    return None if inside else (lat, lon)


def refuse_out_of_region(intent: Intent, lat: float, lon: float) -> PlannerOutput:
    """Decline honestly, naming the place and the limit."""
    name = intent.spatial_reference.name if intent.spatial_reference else "that location"
    return _refuse(
        intent,
        RefusalReason.OUT_OF_REGION,
        f"{name} lies outside the area ORCA covers, which runs from Point Calimere "
        f"up to Cuddalore and south to Rameswaram. There is no forecast or "
        f"satellite data cached for it, so any answer would be invented.",
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
    elif intent.query_type is QueryType.CONDITIONS_REPORT:
        pass  # place-only by design; no vessel gate on a report
    else:
        subs["VESSEL_CLASS"] = VesselClass.FRP_9M.value  # non-safety: harmless default
    return subs


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def plan_query(
    query: str,
    context: Intent | None = None,
    history: list[tuple[str, str]] | None = None,
    dialogue: list[dict[str, str]] | None = None,
    awaiting: str | None = None,
) -> PlanningResult:
    """Plan a user query. Returns exactly one of plan / clarification / refusal.

    Never raises on model failure. If the model is unreachable, the hardcoded
    fallback plan is used when the intent can be inferred from context; if
    it cannot, the result is an honest clarification.
    """
    notes: list[str] = []

    # Gate 0: a greeting carries no request, so it inherits no slots.
    #
    # It does NOT skip the model. This gate used to answer greetings itself
    # from a fixed paragraph, which was wrong twice over: the reply was
    # identical every time regardless of what was asked, and the list of words
    # it recognised was a lookup table pretending to be comprehension -- it had
    # no entry for "alloo", so "alloo" fell through to the model and got a
    # markedly better answer than "hello" did. That is the whole argument.
    #
    # What survives is the part that was actually load-bearing: after a
    # fishing-zone question, typing "hello" returned a full fishing-zone answer
    # (2026-09-06), because the model was shown a previous turn and continued
    # it. So a greeting still gets the conversation for *reference*, and no
    # `carry_these_slots` for *inheritance*. It may talk about what came
    # before; it may not silently re-ask it.
    is_filler = keyword_intent.is_conversational_filler(query)
    if is_filler:
        notes.append("greeting: conversation shown for reference, slots withheld")

    # Cheap first: is this even a question for the planner?
    routed = _route(query, dialogue, awaiting)
    if routed is not None:
        return PlanningResult(
            output=routed.output,
            attempts=routed.attempts,
            llm_provider=routed.llm_provider,
            llm_model=routed.llm_model,
            notes=notes + routed.notes,
        )

    system = _system_prompt()
    user = _user_message(
        query, None if is_filler else context, history, is_filler, dialogue
    )

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
            # The model is reachable -- it answered twice, we just could not
            # use the shape it answered in. Dropping to the keyword table here
            # was wrong: on 2026-09-07 three turns in one session, including
            # "what is safest route for my vessel" and "fishing zones ??",
            # were understood by the model and then answered with a canned
            # paragraph about chlorophyll, because a working model was
            # discarded over a schema error.
            #
            # So try the model once more with the structure taken away. If it
            # can classify, we get a plan; if it can only talk, we get a reply
            # in its own words. Either beats a lookup table.
            spoken = _chat_via_llm(query, dialogue, notes)
            if spoken is not None:
                return spoken
            return _plan_without_llm(query, context, notes, attempts + 1)

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
    # Gate 0b: a chat turn is finished here.
    #
    # It inherits nothing, fills no slots and runs no gate, because there is
    # nothing to gate: no tool will be called and no number will be claimed.
    # Passing it through the slot gate is exactly the old bug -- a question
    # about what ORCA does came back as "which landing centre are you leaving
    # from?", because every state that was not a plan was treated as an
    # unfinished safety question.
    if output.state == "chat" and output.chat is not None:
        return PlanningResult(
            output=_chat(query, output.chat.text, output.chat.suggestions),
            attempts=attempts,
            llm_provider=first.provider,
            llm_model=first.model,
            notes=notes + ["answered conversationally; no tools called"],
        )

    intent = apply_inheritance(output.intent, context)
    output = output.model_copy(update={"intent": intent})

    # Gate 1a: the reporting/advising fence, applied in code. The prompt tells
    # the model that safety phrasing means safety_assess, but if it returns a
    # conditions report for "is it safest to go out" anyway, the vessel gate
    # must still fire. Rewriting the type here (rather than refusing) is
    # correct: the question is real, it just needs a boat before it can run.
    if intent.query_type is QueryType.CONDITIONS_REPORT and keyword_intent.safety_phrasing(
        intent.raw_query or query
    ):
        notes.append(
            "model returned conditions_report for safety phrasing; "
            "re-routed to safety_assess so the vessel gate fires"
        )
        intent = intent.model_copy(update={"query_type": QueryType.SAFETY_ASSESS})
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

    # Gate 1c: the place must be inside the box. Code decides this, not the
    # model -- a prompt instruction is not a boundary check.
    if intent.spatial_reference is not None:
        away = outside_box(intent.spatial_reference.name)
        if away is not None:
            notes.append(
                f"{intent.spatial_reference.name} is outside the study area; refused in code"
            )
            return PlanningResult(
                output=refuse_out_of_region(intent, *away),
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
    # A greeting carries no request, so planning it from context manufactures
    # one. Found live on 2026-09-06: after a fishing-zone question, typing
    # "hello" returned a full fishing-zone answer. With a model the router
    # answers conversationally; without one the honest answer is the offline
    # chat message, not a re-run of the previous question.
    if keyword_intent.is_conversational_filler(query):
        notes = notes + ["no LLM; greeting answered conversationally, slots withheld"]
        return PlanningResult(
            output=_chat(
                query,
                "I cannot reach my language model at the moment, so I can only "
                "take the standard questions right now. " + CAPABILITIES,
            ),
            attempts=attempts,
            notes=notes,
        )
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

        # The genuinely model-free tier: no provider reachable, no conversation
        # behind us, and the keywords did not commit.
        #
        # This message used to say "I did not catch what you need", which was
        # untrue in the case that actually occurred -- the model had understood
        # the question fine and a schema error had thrown its answer away. That
        # path now retries conversationally (`_chat_via_llm`) and only reaches
        # here when there is really nothing to ask. So the message can be
        # honest about which failure this is.
        return PlanningResult(
            output=_chat(
                query,
                "I cannot reach my language model at the moment, so I can only "
                "take the standard questions right now. " + CAPABILITIES,
            ),
            attempts=attempts,
            notes=notes + ["no LLM, no context, and keywords were inconclusive"],
        )
    # With context, the slots carry -- but the QUERY TYPE must be re-read from
    # this turn's words. Copying the previous type wholesale is how "and where
    # are the fish?" came back as another safety verdict: the LLM was rate
    # limited, this path ran, and it answered the previous question again.
    #
    # Keywords are a poor classifier and a far better one than "whatever was
    # asked last time". When they are inconclusive the previous type is still
    # the best guess available, and the note says which happened.
    guess = keyword_intent.classify(query)
    updates: dict[str, Any] = {
        "raw_query": query,
        "inherited_slots": ["spatial_reference", "vessel_class"],
    }
    if guess is not None and guess.query_type is not context.query_type:
        updates["query_type"] = guess.query_type
        notes = notes + [
            f"no LLM; re-classified as {guess.query_type.value} from keywords "
            f"{guess.matched} rather than carrying {context.query_type.value} forward"
        ]
    else:
        notes = notes + [
            f"no LLM; keywords inconclusive, carrying {context.query_type.value} "
            "forward from the previous turn"
        ]

    # A new place named in this turn replaces the carried one.
    place = keyword_intent.extract_place(query)
    if place and (
        context.spatial_reference is None or place != context.spatial_reference.name
    ):
        updates["spatial_reference"] = SpatialReference(name=place)
    vessel = keyword_intent.extract_vessel_class(query)
    if vessel and (
        context.vessel_class is None or vessel != context.vessel_class.value
    ):
        updates["vessel_class"] = VesselClass(vessel)

    intent = context.model_copy(update=updates)
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
