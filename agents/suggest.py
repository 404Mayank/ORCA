"""Post-answer follow-up suggestions, contextual to the turn just answered.

Planner-written suggestions are pre-answer guesses: they cannot reference
anything the tools discovered. This module runs **after** ``verify()``
passes, on the verified recommendation plus the intent, and proposes up to
four follow-up questions that continue *this* answer rather than restarting
the conversation.

Guards, all in code:

* Every model-written suggestion passes through ``strip_numbers`` -- the
  same guard as deliberation concerns. A suggestion button sends a query,
  and a digit in it would smuggle an unverified figure into the next turn.
* Remnants under 12 characters are dropped (the floor is borrowed from
  ``agents/deliberate.py::_MIN_CONCERN_CHARS`` -- a stripped fragment is
  not a question).
* Slot values go through ``VESSEL_LABEL`` and the phrase rule before they
  reach a template, so ``frp_9m`` and "next 2 days" can never leak digits.
* The fallback chain is explicit and labelled: model -> rule bank ->
  static list. ``mixed`` means the final button set was drawn from more
  than one source, in any combination -- never the single dominant source.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agents.deliberate import strip_numbers
from agents.grounding import grounded

__all__ = [
    "MAX_SUGGESTIONS",
    "SUGGEST_BUDGET_S",
    "VESSEL_LABEL",
    "Suggestions",
    "apply_hygiene",
    "clean_daypart",
    "humanize_vessel",
    "parse_model_suggestions",
    "rule_suggestions",
    "suggest_followups",
]

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "suggest.md"

#: Buttons the UI can show without scrolling pagers. Matches the planner cap.
MAX_SUGGESTIONS = 4

#: How long the answer path waits for model-written suggestions before
#: serving rule-based ones. See ``orchestrator/turn.py`` for the wait
#: semantic (ships at max(narration_done, min(suggest_done, 6s))).
SUGGEST_BUDGET_S = 6.0

#: Below this a stripped suggestion is a fragment, not a question. Borrowed
#: from ``agents/deliberate.py::_MIN_CONCERN_CHARS``, stated not invented.
MIN_SUGGESTION_CHARS = 12

#: Enum values are slot keys, not words a fisherman uses. ``frp_9m``
#: contains a digit, so raw interpolation would defeat the numbers ban --
#: every vessel value is mapped before it reaches a template or a prompt.
VESSEL_LABEL = {
    "kattumaram": "kattumaram",
    "frp_9m": "FRP boat",
    "mechanised_trawler": "mechanised trawler",
}

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def humanize_vessel(value: str | None) -> str | None:
    """A vessel enum value as words, or None when unknown."""
    if not value:
        return None
    return VESSEL_LABEL.get(value, value.replace("_", " "))


def clean_daypart(phrase: str | None) -> str:
    """A user time phrase made safe for interpolation.

    A clean phrase ("tomorrow morning") passes through. A phrase carrying
    digits ("next 2 days") degrades to a fixed daypart-free variant -- never
    the mangled remnant ("next  days"), which reads as a typo.
    """
    if not phrase or not phrase.strip():
        return "the coming days"
    text = phrase.strip()
    if strip_numbers(text).strip() == text:
        return text
    return "the coming days"


def _slot_place(intent) -> str | None:
    ref = getattr(intent, "spatial_reference", None)
    name = getattr(ref, "name", None) if ref else None
    return name.strip() if name and name.strip() else None


def _context(intent) -> dict[str, str | None]:
    """The only facts a suggestion may reference: already verified."""
    vessel = getattr(intent, "vessel_class", None)
    window = getattr(intent, "time_window", None)
    qtype = getattr(intent, "query_type", None)
    return {
        "query_type": getattr(qtype, "value", None) or str(qtype) if qtype else None,
        "place": _slot_place(intent),
        "vessel": humanize_vessel(getattr(vessel, "value", None) or (str(vessel) if vessel else None)),
        "daypart": clean_daypart(getattr(window, "phrase", None) if window else None),
    }


#: Deterministic slot templates -- same standing as the existing
#: ``SUGGESTIONS`` / ``_CLARIFICATION_QUESTIONS`` lists in code, not model
#: instructions. Every slot they interpolate is already number-free.
RULE_TEMPLATES: dict[str, list[str]] = {
    "safety_assess": [
        "What is the safest window for my {vessel} from {place}?",
        "When should my {vessel} be back at {place}?",
        "Is {place} safe for a {vessel} {daypart}?",
        "What should my {vessel} watch for off {place}?",
    ],
    "pfz_locate": [
        "Where is the nearest fishing zone from {place}?",
        "How do I reach the zone from {place}?",
        "Is the zone still good {daypart}?",
        "Which zones must I avoid near {place}?",
    ],
    "geofence_check": [
        "Which zones must I avoid near {place}?",
        "Where is the boundary line from {place}?",
        "Is it safe to take my {vessel} out from {place}?",
        "What should my {vessel} watch for off {place}?",
    ],
    "causal_explain": [
        "Why has my catch declined off {place}?",
        "Is the zone still good {daypart}?",
        "Where is the nearest fishing zone from {place}?",
        "What should my {vessel} watch for off {place}?",
    ],
    "conditions_report": [
        "What are the weather and sea conditions near {place}?",
        "Is {place} safe for a {vessel} {daypart}?",
        "When should my {vessel} be back at {place}?",
        "What should my {vessel} watch for off {place}?",
    ],
}

_GENERIC_RULES = [
    "Is it safe to go out from {place} {daypart}?",
    "Where is the nearest fishing zone from {place}?",
    "What are the weather and sea conditions near {place}?",
    "Why has my catch declined off {place}?",
]

#: Slot-free floor: fires when no place or vessel is known, so an answer
#: turn never ships with zero buttons.
_SLOT_FREE_RULES = [
    "Is it safe to go out tomorrow morning?",
    "Where is the nearest fishing zone?",
    "What are the weather and sea conditions near the coast?",
    "Why has the catch changed this season?",
]


def rule_suggestions(intent) -> list[str]:
    """Contextual suggestions with no model involved.

    Templates needing a missing slot are skipped; the remainder are filled
    from the number-free context. Place names come from the gazetteer via
    the intent and carry no digits (asserted in tests).
    """
    ctx = _context(intent)
    templates = RULE_TEMPLATES.get(ctx["query_type"] or "", []) + _GENERIC_RULES
    out: list[str] = []
    for template in templates:
        needed = set(re.findall(r"\{(\w+)\}", template))
        if any(not ctx.get(slot) for slot in needed):
            continue
        out.append(template.format(**{slot: ctx[slot] for slot in needed}))
        if len(out) >= MAX_SUGGESTIONS * 2:  # headroom for de-repeat filtering
            break
    if not out:
        out = list(_SLOT_FREE_RULES)
    return out


def parse_model_suggestions(text: str) -> list[str]:
    """Tolerant parse of the model's reply: bare list, {"suggestions": [...]},
    or fenced JSON. Anything else is an empty list, never an exception."""
    if not text or not text.strip():
        return []
    raw = text.strip()
    fence = _FENCE.search(raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("suggestions", [])
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, (str, int, float)) and not isinstance(item, bool)]


def answer_numbers(recommendation) -> list[float]:
    """Every figure the verified answer actually contains.

    A tighter pool than the one deliberation uses. A suggestion is a button a
    fisherman taps, and it is read as continuous with the answer above it, so
    it may only reference numbers that answer really made -- not everything
    some tool happened to return on the way.
    """
    pool: list[float] = []
    for block in ("claims", "drivers", "negative_findings", "operational_guidance",
                  "alternatives", "hypotheses"):
        for item in getattr(recommendation, block, None) or []:
            slots = getattr(item, "slots", None) or {}
            pool.extend(
                float(v) for v in slots.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            )
            evaluation = getattr(item, "evaluation", None)
            threshold = getattr(evaluation, "threshold", None) if evaluation else None
            if threshold is not None and isinstance(getattr(threshold, "value", None), (int, float)):
                pool.append(float(threshold.value))
    return pool


def apply_hygiene(items: list[str], pool: list[float] | None = None) -> list[str]:
    """Check numbers, drop fragments, order-preserving dedupe, cap.

    Runs on model output AND on rule/static backfill candidates, because a
    template filled from a future slot and a static string are both capable
    of carrying a digit the day someone edits them.

    ``pool`` is the set of figures the answer itself made. A suggestion
    carrying any other number is dropped whole rather than mangled -- see
    :mod:`agents.grounding`. Omitting the pool means no number is grounded,
    which is the right default for a caller with no answer in hand.
    """
    seen: dict[str, None] = {}
    for item in items:
        cleaned, _ = grounded(str(item).strip(), pool or [])
        if not cleaned or len(cleaned) < MIN_SUGGESTION_CHARS:
            continue
        # Left in place as belt and braces. These patterns were the wreckage
        # digit-stripping used to leave behind ("the next  days"), which no
        # longer happens -- nothing is edited now, only kept or dropped. They
        # still catch a genuinely malformed static string.
        if "  " in cleaned or re.search(r"-\s*(day|days|week|hour|kn|m)\b", cleaned):
            continue
        if re.search(r"\b(next|last|past|over|under|about|around|within|above|below)\s+(days?|weeks?|hours?|minutes?|km)\b", cleaned):
            continue
        if cleaned not in seen:
            seen[cleaned] = None
        if len(seen) >= MAX_SUGGESTIONS:
            break
    return list(seen)


@dataclass(frozen=True)
class Suggestions:
    texts: list[str]
    source: str
    """model | rules | static | mixed. ``mixed`` means the final button set
    was drawn from more than one source, in any combination."""


def _assemble(
    candidates: list[tuple[list[str], str]], pool: list[float] | None = None
) -> Suggestions:
    """Merge ordered (items, source) layers into a capped button set.

    Layers apply in order; the first layer is primary. Every layer is
    hygiene-filtered against ``pool`` -- the figures the answer itself made.
    ``mixed`` iff more than one layer contributed a button that survived.
    """
    picked: list[str] = []
    used: list[str] = []
    for items, source in candidates:
        fresh = [t for t in apply_hygiene(items, pool) if t not in picked]
        if fresh:
            picked.extend(fresh[: MAX_SUGGESTIONS - len(picked)])
            used.append(source)
        if len(picked) >= MAX_SUGGESTIONS:
            break
    picked = picked[:MAX_SUGGESTIONS]
    if not used:
        return Suggestions(texts=[], source="static")
    if len(used) > 1:
        return Suggestions(texts=picked, source="mixed")
    return Suggestions(texts=picked, source=used[0])


def suggest_followups(recommendation, intent, prior_options: list[str] | None = None) -> Suggestions:
    """Propose follow-up questions for a verified answer. Never raises.

    Chain: model (post-verify, number-stripped) -> rule bank -> the static
    ``SUGGESTIONS`` floor. Prior-turn options filter every layer, so a
    follow-up never repeats the button it just answered.
    """
    from agents.intent_planner_agent import SUGGESTIONS
    from orchestrator.llm import client as llm

    prior = [p for p in (prior_options or []) if p]
    # The answer's own figures. A suggestion may quote one of these back --
    # "is it still safe if the gusts hold at 18.7 kn?" is a question worth
    # offering -- and may not invent any other.
    pool = answer_numbers(recommendation)
    rules = [t for t in apply_hygiene(rule_suggestions(intent), pool) if t not in prior]
    static = [t for t in apply_hygiene(list(SUGGESTIONS), pool) if t not in prior]

    if not any(llm.provider_status().values()):
        # No provider configured: the model is never attempted. The answer
        # is the deterministic floor, honestly labelled.
        return _assemble([(static, "static")], pool)

    try:
        ctx = _context(intent)
        verdict = getattr(getattr(recommendation, "verdict", None), "value", None)
        asked = ", ".join(prior) if prior else "none yet"
        user = (
            f"Query type: {ctx['query_type']}; place: {ctx['place'] or 'unknown'}; "
            f"vessel: {ctx['vessel'] or 'unknown'}; daypart: {ctx['daypart']}; "
            f"verdict: {getattr(verdict, 'value', verdict) or 'none'}. "
            f"Asked already (do not repeat): {asked}."
        )
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        result = llm.complete("suggest", system=prompt, user=user)
    except Exception:  # noqa: BLE001 -- suggestions never break an answer
        result = None

    model_texts: list[str] = []
    model_ok = result is not None and getattr(result, "ok", False)
    if model_ok:
        model_texts = [
            t for t in apply_hygiene(parse_model_suggestions(result.text), pool)
            if t not in prior
        ]

    if model_ok and model_texts:
        return _assemble([(model_texts, "model"), (rules, "rules"), (static, "static")], pool)
    if model_ok:
        # The model answered but nothing survived hygiene: its voice is
        # absent from the buttons, so the set is rules-led, not mixed.
        return _assemble([(rules, "rules"), (static, "static")], pool)
    return _assemble([(rules, "rules"), (static, "static")], pool)
