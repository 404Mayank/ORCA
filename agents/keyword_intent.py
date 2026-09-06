"""Last-resort deterministic intent classification, for when no LLM exists.

CLAUDE.md plans for the venue losing internet with an Ollama tier. That covers
"no network". It does not cover "no model at all" -- no key, no Ollama
installed -- and in that state the planner could not classify a question even
as well as a keyword match, so every question became a clarification and the
API could not answer anything.

This module is the tier below Ollama. It is **not** the LLM interpreting; it is
a crude substitute that knows it is crude, and everything about how it is wired
reflects that:

* It proposes a **query type**, and matches a place name and vessel class
  against the gazetteer and the configured classes. It **never produces a
  coordinate** -- ``resolve_place`` does that, from the gazetteer, exactly as
  it does on the LLM path. Anything not matched is left empty so the existing
  slot gates still fire and the user is asked.
* It returns a **confidence**, and a weak match is reported as no match. Two
  scoring words are required before it will commit to anything, because a
  single stray "fish" in "is it safe to fish tomorrow" must not outrank the
  safety reading.
* Every use is recorded in ``notes`` so the trace shows the answer was routed
  by keywords rather than by a model.

The governing rule is intact: this decides *what to look up*, exactly like the
planner does, and code still decides what is true. What it must never become is
a way of avoiding the LLM when one is available -- it is only consulted after
every provider has failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.schemas.intent import QueryType

__all__ = [
    "KeywordMatch",
    "classify",
    "extract_place",
    "extract_vessel_class",
    "MIN_SCORE",
]

#: Two independent signals before the classifier commits. One word is a
#: coincidence; "safe" plus "venture" is a question about going to sea.
MIN_SCORE = 2

#: Scored terms per query type. Weighted, because some words are decisive and
#: others are merely suggestive: "cyclone" all but settles a safety question,
#: while "today" settles nothing.
_TERMS: dict[QueryType, dict[str, int]] = {
    QueryType.SAFETY_ASSESS: {
        "safe": 2, "safety": 2, "safest": 2, "danger": 2, "dangerous": 2, "risk": 2,
        "cyclone": 3, "storm": 2, "warning": 2, "alert": 2, "rough": 2,
        "venture": 2, "go out": 2, "going out": 2, "head out": 2, "sail": 1,
        "sea": 1, "weather": 1, "wave": 1, "waves": 1, "wind": 1, "tomorrow": 1,
    },
    QueryType.PFZ_LOCATE: {
        "pfz": 3, "fishing zone": 3, "fishing ground": 3, "potential fishing": 3,
        "where": 2, "nearest": 2, "catch fish": 2, "find fish": 2,
        "shoal": 2, "school": 1, "fish": 1, "fishing": 1,
    },
    QueryType.GEOFENCE_CHECK: {
        "boundary": 3, "imbl": 3, "maritime boundary": 3, "border": 3,
        "sri lanka": 2, "arrest": 2, "avoid": 2, "restricted": 2,
        "protected": 2, "marine park": 3, "mpa": 3, "sanctuary": 2,
        "eez": 3, "zone": 1, "legal": 2, "permitted": 2,
    },
    QueryType.CAUSAL_EXPLAIN: {
        "why": 3, "declined": 3, "decline": 3, "dropped": 2, "falling": 2,
        "fallen": 2, "less fish": 3, "fewer fish": 3, "poor catch": 3,
        "no fish": 2, "reason": 2, "explain": 2, "cause": 2,
        "productivity": 2, "used to": 2,
    },
    # Read-only conditions: tide, weather and alert status. Deliberately
    # weaker than the safety set on every shared word ("weather" 2 vs the
    # safety set's breadth, "alerts" plural here vs "alert" singular
    # there), so that any genuine safety phrasing outscores it -- and the
    # fence in classify() makes that precedence absolute, not marginal.
    QueryType.CONDITIONS_REPORT: {
        "tide": 3, "high tide": 3, "low tide": 3, "tide table": 3,
        "sea conditions": 3, "sea state": 2, "conditions": 2,
        "weather": 2, "swell": 2, "seas": 2, "route": 2,
        "alerts": 2, "advisory": 2, "advisories": 2, "visibility": 1,
    },
}


@dataclass(frozen=True)
class KeywordMatch:
    query_type: QueryType
    score: int
    matched: list[str]
    runner_up: QueryType | None = None
    runner_up_score: int = 0

    @property
    def decisive(self) -> bool:
        """Whether the winner clearly beat the alternative.

        A one-point margin between "why has the catch dropped" and "where are
        the fish" is not a classification, it is a coin toss, and a coin toss
        should become a clarification rather than an answer to a question
        nobody asked.
        """
        return self.score >= MIN_SCORE and self.score > self.runner_up_score


def _score(text: str, terms: dict[str, int]) -> tuple[int, list[str]]:
    total, matched = 0, []
    for term, weight in terms.items():
        # Word-boundary matching, so "sea" does not fire inside "season" and
        # "mpa" does not fire inside "company".
        if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            total += weight
            matched.append(term)
    return total, matched


def safety_phrasing(query: str) -> bool:
    """Whether the query uses safety-decisive language.

    Decisive means a single matched term of weight 2 or more ("safe",
    "safest", "venture", "go out", "cyclone" ...) -- not an accumulation
    of weak ones. "Weather" plus "sea" sums to 2 and is still just a
    description of conditions; treating it as a safety question would make
    the fence eat the report type it is meant to protect.

    The fence between reporting and advising. A conditions report must never
    answer a question about whether it is safe to go out -- the vessel gate
    exists precisely for those -- so safety-decisive language routes to
    ``SAFETY_ASSESS`` even when conditions keywords also match ("safest
    route" scores both). Used by :func:`classify` and by the planner gate in
    ``agents.intent_planner_agent``; the rule lives here so the two cannot
    drift.
    """
    text = query.lower().strip()
    terms = _TERMS[QueryType.SAFETY_ASSESS]
    return any(
        weight >= 2 and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)
        for term, weight in terms.items()
    )


def classify(query: str) -> KeywordMatch | None:
    """Best-effort query type from keywords, or None when unsure.

    None is the common and correct outcome for a vague question. The caller
    must turn it into a clarification, never into a guess.
    """
    text = query.lower().strip()
    if not text:
        return None

    scored = []
    for query_type, terms in _TERMS.items():
        total, matched = _score(text, terms)
        if total:
            scored.append((total, query_type, matched))
    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_type, best_matched = scored[0]
    runner_up_score, runner_up = (scored[1][0], scored[1][1]) if len(scored) > 1 else (0, None)

    if safety_phrasing(query):
        # The fence: safety-decisive language outranks a conditions win at any
        # margin, and breaks a tie involving conditions. "Safest route" ties
        # 2-2 on keywords; answering it as a vessel-free report would bypass
        # the safety gate, so it becomes the safety question it is (and the
        # slot gate then asks for place and boat).
        safety_total, safety_matched = _score(text, _TERMS[QueryType.SAFETY_ASSESS])
        safety = KeywordMatch(
            query_type=QueryType.SAFETY_ASSESS,
            score=safety_total,
            matched=sorted(safety_matched),
            runner_up=best_type,
            runner_up_score=best_score,
        )
        if best_type is QueryType.CONDITIONS_REPORT:
            return safety
        tied = [t for s, t, _ in scored if s == best_score]
        if best_score <= safety_total and QueryType.CONDITIONS_REPORT in tied:
            return safety

    match = KeywordMatch(
        query_type=best_type,
        score=best_score,
        matched=sorted(best_matched),
        runner_up=runner_up,
        runner_up_score=runner_up_score,
    )
    return match if match.decisive else None


# --------------------------------------------------------------------------
# Slot extraction -- deliberately narrow
# --------------------------------------------------------------------------
#
# Matching the *word* "Nagapattinam" against the gazetteer is a lookup, not an
# interpretation, and it is strictly safer than what the rule forbids: the LLM
# may say the word but may never produce the coordinate, and here no model is
# involved at all. resolve_place still turns the name into numbers.
#
# What this cannot do is read negation or hypotheticals. "I am NOT going to
# Nagapattinam" and "if I were at Rameswaram" both match. A crude guard covers
# the common negation; beyond that the mitigation is that an extracted slot is
# recorded in `inherited_slots`, so it surfaces to the user as a stated
# assumption they can correct rather than as a silent decision.

#: Words that, immediately before a place, invert its meaning. Not exhaustive
#: -- it cannot be -- but it catches the phrasings that actually occur.
#: "avoid" is deliberately NOT here. In this domain it refers to zones ("which
#: zones must I avoid near Rameswaram"), not to the departure point, and
#: including it made the geofence query lose its own place name.
_NEGATIONS = ("not ", "n't ", "away from", "instead of", "rather than", "except")


def _negated(text: str, term: str) -> bool:
    """Whether a negation word sits just before ``term``."""
    match = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)
    if match is None:
        return False
    # Narrow window: a negation four words back is usually about something
    # else in the sentence.
    preceding = text[max(0, match.start() - 14) : match.start()]
    return any(word in preceding for word in _NEGATIONS)


def extract_place(query: str) -> str | None:
    """A gazetteer place name mentioned in the query, or None.

    Only names the gazetteer already knows are matched. An unknown place is
    None rather than a guess, because ``resolve_place`` would fail on it anyway
    and asking is better than failing.
    """
    from core import config

    text = query.lower()
    points = config.load_yaml("bbox.yaml").get("reference_points", {})
    best: tuple[int, str] | None = None
    for key in points:
        name = key.replace("_", " ")
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text) and not _negated(text, name):
            # Longest match wins, so "point calimere" beats a stray "point".
            if best is None or len(name) > best[0]:
                best = (len(name), key)
    return best[1].replace("_", " ").title() if best else None


#: Phrasings a fisherman actually uses, mapped to the config's class ids.
_VESSEL_TERMS: dict[str, tuple[str, ...]] = {
    "kattumaram": ("kattumaram", "catamaran", "vallam", "traditional boat", "country boat"),
    "frp_9m": ("frp", "fibreglass", "fiberglass", "outboard", "obm"),
    "mechanised_trawler": ("trawler", "mechanised", "mechanized", "inboard"),
}


def extract_vessel_class(query: str) -> str | None:
    """A vessel class named in the query, or None.

    None when two classes are mentioned: an ambiguous boat is asked about, not
    picked, because the class sets the thresholds the verdict comes from.
    """
    text = query.lower()
    hits = {
        vessel_class
        for vessel_class, terms in _VESSEL_TERMS.items()
        if any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", text) for t in terms)
    }
    return hits.pop() if len(hits) == 1 else None


# --------------------------------------------------------------------------
# Conversational filler
# --------------------------------------------------------------------------
#
# A greeting is not a follow-up question, and the difference matters because of
# context inheritance. Found live on 2026-09-06: after a fishing-zone question,
# typing "hello" returned a full fishing-zone answer. The model saw the previous
# turn in its prompt, had nothing else to go on, and continued it.
#
# The obvious guard -- "require a marine keyword" -- is wrong. "What about the
# day after?" is a genuine follow-up with no marine keyword in it, and refusing
# that would break multi-turn conversation, which is a stated requirement.
#
# The real distinction is narrower: a greeting carries **no request at all**.
# So the test is not "does this mention the sea" but "is this the whole of the
# user's turn, and is it only a pleasantry".

#: Whole-utterance pleasantries. Matched against the entire query, never as a
#: substring, so "hi" cannot fire inside "is it high tide".
_FILLER = frozenset(
    {
        "hi", "hello", "hey", "yo", "hai",
        "good morning", "good afternoon", "good evening", "good day",
        "vanakkam", "namaste", "namaskaram",
        "thanks", "thank you", "thankyou", "ta", "cheers",
        "ok", "okay", "k", "fine", "good", "nice", "great", "cool",
        "yes", "no", "yeah", "yep", "nope", "sure",
        "bye", "goodbye", "see you", "test", "testing",
        "help", "hello?", "you there", "are you there",
    }
)


def is_conversational_filler(query: str) -> bool:
    """Whether a turn is a greeting rather than a request.

    True only when the **entire** utterance is a pleasantry. A greeting
    attached to a real question -- "hello, is it safe tomorrow?" -- is a real
    question and must not be caught here.

    The caller uses this to refuse *context inheritance*, not to refuse the
    user: the answer to a greeting is to ask what they need, which is what the
    clarification path already does well.
    """
    text = query.strip().lower().strip(".!?,")
    if not text:
        return True
    if text in _FILLER:
        return True
    # "hello there", "hi again" -- a greeting plus a filler word, nothing more.
    words = text.split()
    return len(words) <= 3 and all(
        word.strip(".!?,") in _FILLER or word in {"there", "again", "orca", "please"}
        for word in words
    )
