"""What the user asked for, as slots.

The intent block is the first half of the single LLM call made by
``agents/intent_planner_agent.py``. The planner block is the other half and
lives in ``plan.py``.

This module owns the enums shared across the whole schema package --
:class:`QueryType` and :class:`VesselClass` -- so that ``recommendation.py`` and
``plan.py`` can both import them without a cycle. Import order inside
``core/schemas`` is:

    units  ->  intent  ->  {plan, recommendation, tool_io}

The governing rule shows up here as :class:`ClarificationRequest`. Interpreting
"tomorrow morning" into a timestamp is language work and the LLM does it. But
*guessing* a location or a vessel class for a safety question is adjudication
wearing a helpful face, and it is forbidden. A missing safety-critical slot
produces a clarification, never a default.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "QueryType",
    "VesselClass",
    "SpatialReference",
    "TimeWindow",
    "Intent",
    "ClarificationRequest",
    "RefusalReason",
    "Refusal",
    "ChatReply",
    "SAFETY_CRITICAL_SLOTS",
]


class QueryType(str, Enum):
    """The five query types.

    ``CONDITIONS_REPORT`` is read-only: tide, weather and alert status for a
    place, with no verdict and no vessel gate. It exists because "what are
    conditions near me" is not a safety assessment -- forcing it through
    ``SAFETY_ASSESS`` demanded a vessel class the user never came for. The
    fence is in code, not in prompts: any safety phrasing ("safe", "venture",
    "go out", "safest") routes to ``SAFETY_ASSESS`` even when conditions
    keywords also match (see ``agents.keyword_intent.safety_phrasing`` and the
    planner gate). Reporting is not advising.
    """

    PFZ_LOCATE = "pfz_locate"
    SAFETY_ASSESS = "safety_assess"
    GEOFENCE_CHECK = "geofence_check"
    CAUSAL_EXPLAIN = "causal_explain"
    CONDITIONS_REPORT = "conditions_report"


class VesselClass(str, Enum):
    """Vessel classes on the South Coromandel coast.

    Thresholds are per class -- a 2.5 m sea is a working day for a mechanised
    trawler and a bad afternoon for a kattumaram. The physical parameters
    (length, cruise speed, limits) live in ``config/risk_thresholds.yaml``
    keyed by these values, not here, so that a judge can read the numbers and
    their citations in one file.

    There is deliberately no ``UNKNOWN`` member. An unknown vessel class is a
    clarification, not a value.
    """

    #: Traditional catamaran / vallam, roughly under 7 m, often rowed or with
    #: a small outboard. The most exposed class.
    KATTUMARAM = "kattumaram"

    #: FRP boat around 9 m with an outboard. The modal Coromandel day-fishing
    #: vessel and the default demo subject.
    FRP_9M = "frp_9m"

    #: Mechanised trawler, roughly 15-20 m, multi-day capable.
    MECHANISED_TRAWLER = "mechanised_trawler"


#: Slots that may never be inferred, defaulted or guessed when the query is a
#: safety assessment. Absence produces a :class:`ClarificationRequest`.
#: This tuple is enforced in ``agents/intent_planner_agent.py`` and asserted in
#: tests, so it is the single place the rule is written down.
SAFETY_CRITICAL_SLOTS: tuple[str, ...] = ("spatial_reference", "vessel_class")


class SpatialReference(BaseModel):
    """Where the question is about.

    Either a named place the geocoder can resolve against the landing-centre
    table, or an explicit coordinate. The LLM may fill ``name`` from the user's
    words; it must never fill ``lat``/``lon`` itself. Coordinates come from
    ``resolve_place()`` in the tool layer, which is why ``resolved_by`` exists.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, description="Place as the user said it, e.g. 'Nagapattinam'."
    )
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    resolved_by: str | None = Field(
        default=None,
        description=(
            "tool_call_id of the resolution step. If lat/lon are set and this "
            "is None, a coordinate was invented and the verifier must reject."
        ),
    )
    radius_km: float | None = Field(
        default=None, gt=0.0, description="Area of interest around the point."
    )

    @model_validator(mode="after")
    def _coords_together(self) -> SpatialReference:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be provided together or not at all.")
        if self.name is None and self.lat is None:
            raise ValueError(
                "A spatial reference needs either a name or a coordinate pair."
            )
        return self

    @property
    def is_resolved(self) -> bool:
        """True once a tool has attached real coordinates."""
        return self.lat is not None and self.resolved_by is not None


class TimeWindow(BaseModel):
    """When the question is about.

    Interpreting "tomorrow morning" into a concrete pair of timestamps is
    language work, so the LLM does it -- but it records the phrase it started
    from in ``phrase`` so the assumption is auditable. All times carry an
    offset; the coast runs on IST and a naive datetime here is a bug waiting
    for a demo.
    """

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    timezone: str = "Asia/Kolkata"
    phrase: str | None = Field(
        default=None,
        description="The user's own words, e.g. 'tomorrow morning'. Auditable.",
    )

    @model_validator(mode="after")
    def _check(self) -> TimeWindow:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError(
                "TimeWindow requires timezone-aware datetimes. A naive "
                "timestamp on this coast is a five-and-a-half hour bug."
            )
        if self.end <= self.start:
            raise ValueError(f"TimeWindow end ({self.end}) is not after start.")
        return self

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


class Intent(BaseModel):
    """The parsed request. First of the two blocks the planner LLM returns."""

    model_config = ConfigDict(extra="forbid")

    query_type: QueryType
    spatial_reference: SpatialReference | None = None
    time_window: TimeWindow | None = None
    vessel_class: VesselClass | None = None

    raw_query: str = Field(description="Verbatim user input, before any processing.")
    language: str = Field(
        default="en",
        description=(
            "Always 'en' in phase one. The field exists so that adding Tamil "
            "is an adapter change and not a schema change."
        ),
    )

    missing_slots: list[str] = Field(
        default_factory=list,
        description="Slot names the LLM could not fill from the query or context.",
    )
    inherited_slots: list[str] = Field(
        default_factory=list,
        description=(
            "Slot names carried forward from a previous turn. Every one of "
            "these must surface in the recommendation's assumptions[] so the "
            "user can see what was assumed on their behalf."
        ),
    )

    def blocking_gaps(self) -> list[str]:
        """Missing slots that forbid execution for this query type.

        Only safety assessments have hard requirements: the governing rule
        says never guess a location or a vessel class for a safety question.
        A PFZ query with no vessel class is answerable; a safety verdict with
        no vessel class is not, because the thresholds are per class.
        """
        if self.query_type is not QueryType.SAFETY_ASSESS:
            return []
        gaps: list[str] = []
        for slot in SAFETY_CRITICAL_SLOTS:
            value = getattr(self, slot, None)
            if value is None:
                gaps.append(slot)
        return gaps


class ClarificationRequest(BaseModel):
    """One of the three states the planner may return.

    Asking is a first-class outcome, not a failure. The question is a
    template with slots like everything else the user reads, so the language
    layer stays uniform.
    """

    model_config = ConfigDict(extra="forbid")

    missing_slots: list[str] = Field(min_length=1)
    question_template: str
    slots: dict[str, str | float | int | None] = Field(default_factory=dict)
    options: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete choices where the slot is an enum, e.g. the vessel "
            "classes. A fisherman picking from three buttons beats a fisherman "
            "typing a boat description."
        ),
    )
    partial_intent: Intent | None = Field(
        default=None,
        description="What was understood so far, so the next turn can resume.",
    )


class RefusalReason(str, Enum):
    """Why a query cannot be answered. Closed set, so the UI can be specific."""

    OUT_OF_REGION = "out_of_region"
    OUT_OF_SCOPE = "out_of_scope"
    NO_DATA = "no_data"
    UNSAFE_TO_ANSWER = "unsafe_to_answer"


class Refusal(BaseModel):
    """The third planner state.

    A refusal is a real answer with a real explanation, not an error. It is
    carried inside the recommendation object -- ``verdict`` is None and this
    block is populated -- so the frontend renders one shape for everything.
    """

    model_config = ConfigDict(extra="forbid")

    reason: RefusalReason
    explanation_template: str
    slots: dict[str, str | float | int | None] = Field(default_factory=dict)
    requested: SpatialReference | None = Field(
        default=None, description="What was asked for, recorded even though refused."
    )
    supported_region: str | None = Field(
        default=None,
        description="What we DO cover, so the refusal is useful rather than a wall.",
    )


class ChatReply(BaseModel):
    """A conversational answer that needs no tools.

    The system used to have three states -- plan, clarification, refusal -- and
    no way to simply *talk*. Every turn that was not one of the four marine
    queries fell through to "which landing centre are you leaving from, and what
    kind of boat is it?", so "hello" was answered with an interrogation and
    typing anything unrecognised restarted it. Found live on 2026-09-07.

    A chat reply is the model speaking in its own voice about what it is and
    what it can do. It carries no verdict, calls no tool, and reaches no
    evidence block, so the governing rule applies with full force: **it may not
    contain a number**. ``strip_numbers`` enforces that at the boundary, for the
    same reason it does on agent concerns -- prose is exactly where an invented
    figure looks most authoritative.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, description="What to say back, in plain English.")
    suggestions: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Questions the user could ask next, rendered as buttons.",
    )


class PlannerOutput(BaseModel):
    """Exactly one of four states, enforced.

    The single LLM call returns this. ``validate_plan()`` runs on ``plan``
    before anything executes; the other two states bypass execution entirely.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    state: Literal["plan", "clarification", "refusal", "chat"]
    clarification: ClarificationRequest | None = None
    refusal: Refusal | None = None
    chat: ChatReply | None = None
    # `plan` is typed as Any here to keep this module free of a plan.py import
    # and preserve the one-directional import order. orchestrator/ re-parses it
    # into a Plan before validation.
    plan: dict | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> PlannerOutput:
        present = {
            "plan": self.plan is not None,
            "clarification": self.clarification is not None,
            "refusal": self.refusal is not None,
            "chat": self.chat is not None,
        }
        if not present[self.state]:
            raise ValueError(f"state is '{self.state}' but the {self.state} block is missing.")
        extra = [k for k, v in present.items() if v and k != self.state]
        if extra:
            raise ValueError(
                f"state is '{self.state}' but these blocks are also populated: {extra}. "
                "The planner returns exactly one state."
            )
        return self
