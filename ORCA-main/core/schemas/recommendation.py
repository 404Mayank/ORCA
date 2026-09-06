"""The recommendation object -- the contract every agent contributes to.

This is the frozen centre of ORCA. It is designed against
``docs/ideal_answers/safety_assess.md``, which was written by hand first. If a
change here cannot round-trip that document, the change is wrong.

Four non-negotiable rules from CLAUDE.md, and where each one is enforced:

1.  **Every value is a range.** :class:`~core.units.Range`, used everywhere a
    measurement appears. There is no float field holding a wave height.
2.  **Every claim has a kind** -- :class:`ClaimKind`. The verifier is strict
    about ``observed``, moderate about ``derived``, lenient about ``inferred``.
3.  **Every claim points at evidence; every evidence entry points at a
    ``tool_call_id``.** Enforced by :meth:`Recommendation._check_evidence_links`
    at construction time, so a dangling reference cannot be built at all.
4.  **Claims are slot templates, never finished prose.** :class:`Templated`.
    Nothing in this module stores an English sentence. Storing
    "Waves are 2.8 m" and translating it later corrupts the number; storing
    ``{"max": 2.8}`` plus a template does not.

The object must survive four shapes: a normal verdict, a causal query with no
verdict, a degraded partial answer after a tool failure, and a refusal. It does
this by making ``verdict`` optional and ``refusal`` first-class rather than by
having four response types.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.schemas.intent import QueryType, Refusal, VesselClass
from core.units import Range, ThresholdEvaluation

__all__ = [
    "SCHEMA_VERSION",
    "Templated",
    "ClaimKind",
    "VerdictValue",
    "Verdict",
    "Trajectory",
    "Driver",
    "TurnBackBasis",
    "Window",
    "NegativeFinding",
    "Claim",
    "Hypothesis",
    "OperationalGuidance",
    "AlternativeCost",
    "Alternative",
    "SpatialContext",
    "Assumption",
    "Confidence",
    "Caveat",
    "EvidenceEntry",
    "VisualLayer",
    "ReasoningStep",
    "Recommendation",
]

#: Bumped on any breaking change. The frontend types are generated from this
#: module and check the version at runtime, so a drift is loud rather than
#: silent.
SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------
# Templated text -- rule 4
# --------------------------------------------------------------------------


class Templated(BaseModel):
    """Text stored as a pattern plus its values, never as a finished sentence.

    ``template`` uses ``str.format`` field syntax: ``"Waves {min}-{max} {unit}"``.
    The real rendering, including number formatting and eventual Tamil, is the
    job of ``language/templates/``. :meth:`render` exists for tests and logs.

    The discipline this enforces is subtle but load-bearing. If a claim stored
    the string "Waves are 1.8-2.2 m", then translating it means passing a
    safety number through a language model, and language models round numbers.
    Storing slots means the numbers are copied, never re-generated.
    """

    model_config = ConfigDict(extra="forbid")

    template: str = Field(min_length=1)
    slots: dict[str, str | float | int | bool | None] = Field(default_factory=dict)

    def render(self) -> str:
        """Fill the template. For tests and logging, not for the UI path."""
        try:
            return self.template.format(**self.slots)
        except KeyError as exc:
            raise ValueError(
                f"Template {self.template!r} references slot {exc} which was not "
                "provided. An unfilled slot reaches the user as a literal brace."
            ) from exc

    def slot_numbers(self) -> list[float]:
        """Every numeric slot value. This is what the verifier walks.

        Booleans are excluded: ``bool`` is a subclass of ``int`` in Python and
        letting True through as 1.0 would have the verifier hunting for a 1 in
        the tool log.
        """
        return [
            float(v)
            for v in self.slots.values()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


class VerdictValue(str, Enum):
    """The answer to "can I go".

    ``MARGINAL`` is the well-supported middle case, not an afterthought. On
    this coast in the northeast monsoon it is the most common honest answer,
    and a system that can only say yes or no is not useful. See the ideal
    answer document.
    """

    GO = "go"
    MARGINAL = "marginal"
    NO_GO = "no_go"


#: Ordered worst-to-best. Used by :meth:`Verdict.downgraded_to` so that
#: degradation can only ever move one direction.
_VERDICT_SEVERITY: dict[VerdictValue, int] = {
    VerdictValue.NO_GO: 0,
    VerdictValue.MARGINAL: 1,
    VerdictValue.GO: 2,
}


class Verdict(BaseModel):
    """The safety call. Produced by ``compute_risk_score()``, never by an LLM.

    ``computed_by`` is required and must name a tool call. This is the single
    most important provenance link in the system: it is the mechanical proof
    that the governing rule was followed for the one number that matters most.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: VerdictValue
    score: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Internal risk score. Shown in the evidence drawer, never in the "
            "spoken answer -- '0.4875' means nothing to a fisherman."
        ),
    )
    band: str = Field(description="Human-facing band label for the drawer.")
    computed_by: str = Field(
        min_length=1,
        description="tool_call_id of the compute_risk_score() call. Required.",
    )
    limiting_driver: str | None = Field(
        default=None, description="id of the driver that set this verdict."
    )

    def downgraded_to(self, worse: VerdictValue, why: str) -> Verdict:
        """Return a copy at the more cautious of the two verdicts.

        Degradation is one-way by construction. When a safety input is missing
        -- the alert check timed out, the wave grid had a hole -- the verdict
        may get worse and may never get better. A partial answer that upgrades
        itself is the exact failure mode that puts a boat in the water on a bad
        day, so the type system refuses to express it.
        """
        if _VERDICT_SEVERITY[worse] >= _VERDICT_SEVERITY[self.value]:
            return self
        return self.model_copy(
            update={"value": worse, "band": f"{self.band} (downgraded: {why})"}
        )


# --------------------------------------------------------------------------
# Drivers -- what moved the verdict
# --------------------------------------------------------------------------


class Trajectory(BaseModel):
    """How a driver changes across the requested window.

    This block exists because of requirement 4 in the ideal answer. Wave height
    off Nagapattinam is 1.8-2.2 m at 06:00 and 2.6-3.1 m at 14:00 -- under the
    limit and over it, the same day, the same driver. A driver modelled as one
    observed-vs-threshold pair forces the answer into a false "safe" or a false
    "unsafe", and the whole point of the marginal verdict disappears.
    """

    model_config = ConfigDict(extra="forbid")

    direction: Literal["improving", "worsening", "steady"]
    breaches_at: datetime | None = Field(
        default=None,
        description="When the driver first crosses its threshold. None if it never does.",
    )
    later: Range | None = Field(
        default=None, description="The value at `later_at`, for the contrast sentence."
    )
    later_at: datetime | None = None
    peak_rate_window: tuple[datetime, datetime] | None = Field(
        default=None,
        description=(
            "When most of the change happens. Drives 'the change is quick -- "
            "most of the build is between 11:00 and 14:00', which is the "
            "sentence that actually changes behaviour."
        ),
    )


class Driver(BaseModel):
    """One factor weighed in the verdict, with its threshold attached.

    The observed value and the limit are held in a single
    :class:`~core.units.ThresholdEvaluation` precisely so that the narration
    layer cannot mention one without the other. "Waves 1.8-2.2 m" is a number;
    "waves 1.8-2.2 m against a 2.5 m limit" is an argument, and only the second
    survives a judge asking "compared to what?".
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Stable key, e.g. 'wave_height'.")
    label_template: str = Field(min_length=1)
    slots: dict[str, str | float | int | bool | None] = Field(default_factory=dict)

    evaluation: ThresholdEvaluation
    at: datetime = Field(description="The instant `evaluation.observed` describes.")
    trajectory: Trajectory | None = None

    evidence: list[str] = Field(
        min_length=1, description="tool_call_ids. A driver with no evidence is an opinion."
    )

    @property
    def breaching(self) -> bool:
        return self.evaluation.breaching

    @property
    def marginal(self) -> bool:
        return self.evaluation.marginal

    def render(self) -> str:
        """Fill the label template. For tests and the English renderer.

        A Driver is not a :class:`Templated` because it carries an evaluation
        rather than being pure text, but it renders the same way -- and it
        renders only the observed side. The threshold is deliberately left to
        the caller so that a renderer cannot accidentally print the number
        without the limit it is judged against.
        """
        try:
            return self.label_template.format(**self.slots)
        except KeyError as exc:
            raise ValueError(
                f"Driver {self.id!r} label template references slot {exc} which "
                "was not provided."
            ) from exc


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


class TurnBackBasis(str, Enum):
    """How ``turn_back`` was arrived at.

    ``COMPUTED_STEAM_TIME`` derives it from distance and vessel cruise speed
    and is what we show a judge. ``FLAT_MARGIN`` is the fallback when speed is
    unknown; it is honest but weaker, and it is recorded rather than hidden so
    the difference is visible in the evidence drawer.
    """

    COMPUTED_STEAM_TIME = "computed_steam_time"
    FLAT_MARGIN = "flat_margin"


class Window(BaseModel):
    """When it is workable, and when to start heading in.

    Three timestamps, not two -- requirement 2 in the ideal answer.
    ``turn_back`` and ``ashore_by`` are different instructions and a fisherman
    needs both: one tells him when to pull gear, the other is the deadline the
    first one is derived from.
    """

    model_config = ConfigDict(extra="forbid")

    opens: datetime
    turn_back: datetime
    ashore_by: datetime
    timezone: str = "Asia/Kolkata"

    basis: str = Field(min_length=1, description="tool_call_id of the source timeseries.")
    limiting_driver: str | None = Field(
        default=None, description="Driver id that closes the window."
    )

    turn_back_basis: TurnBackBasis
    steam_distance_km: float | None = Field(
        default=None, ge=0.0, description="Distance back to port, for the computed case."
    )
    vessel_speed_kn: float | None = Field(
        default=None, gt=0.0, description="Cruise speed used in the computation."
    )
    steam_time_h: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _check(self) -> Window:
        if not (self.opens < self.turn_back <= self.ashore_by):
            raise ValueError(
                f"Window must satisfy opens < turn_back <= ashore_by; got "
                f"{self.opens} / {self.turn_back} / {self.ashore_by}."
            )
        for name in ("opens", "turn_back", "ashore_by"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"Window.{name} must be timezone-aware.")
        if self.turn_back_basis is TurnBackBasis.COMPUTED_STEAM_TIME:
            missing = [
                f
                for f in ("steam_distance_km", "vessel_speed_kn", "steam_time_h")
                if getattr(self, f) is None
            ]
            if missing:
                raise ValueError(
                    "turn_back_basis is COMPUTED_STEAM_TIME but the inputs that "
                    f"would make it defensible are missing: {missing}. Use "
                    "FLAT_MARGIN and say so, rather than claiming a computation "
                    "that did not happen."
                )
        return self

    @property
    def workable_hours(self) -> float:
        return (self.turn_back - self.opens).total_seconds() / 3600.0


# --------------------------------------------------------------------------
# Findings, claims, hypotheses
# --------------------------------------------------------------------------


class NegativeFinding(Templated):
    """Something we checked and did not find. First-class, not a missing field.

    "No cyclone active in the Bay of Bengal" and "no INCOIS high-wave warning
    in force" are the two most reassuring sentences in a safety answer. If the
    absence of an alert were modelled as an empty list somewhere, they would
    silently vanish and the answer would get worse.

    ``checked_by`` is required for exactly this reason: when a judge asks "how
    do you know there is no cyclone", the answer is a tool call, not a shrug.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    checked_by: str = Field(min_length=1, description="tool_call_id of the check.")
    authority: str | None = Field(
        default=None, description="Who would have issued it -- IMD, INCOIS."
    )
    valid_until: datetime | None = None


class ClaimKind(str, Enum):
    """Provenance class of a claim. The verifier treats each differently.

    This is about **where the number came from, not how certain the world is**.
    A forecast wave height is ``OBSERVED`` -- it was read verbatim out of a
    tool's output and the verifier can find it in the log. That says nothing
    about whether the forecast is right.

    Getting this distinction written down matters more than it looks. Without
    it, one developer labels forecasts ``inferred`` because the future is
    uncertain, another labels them ``observed`` because they came from a file,
    and the verifier's strictness rules stop meaning anything by Wednesday.
    """

    #: Read directly from a tool's output. Every number must appear verbatim in
    #: the referenced tool call's log. Strictest verification.
    OBSERVED = "observed"

    #: Produced by a deterministic rule over observed values -- a sea-state
    #: band, a gradient window. Numbers must trace to inputs; ``derived_by``
    #: names the rule.
    DERIVED = "derived"

    #: A hypothesis in natural language, produced by the LLM. May carry no
    #: numbers at all. If it does carry a number, that number is held to the
    #: observed standard or the claim is dropped.
    INFERRED = "inferred"


class Claim(Templated):
    """One statement in the answer, with its provenance and its evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: ClaimKind
    evidence: list[str] = Field(default_factory=list)
    derived_by: str | None = Field(
        default=None,
        description="Rule name for DERIVED claims, e.g. 'rule:sea_state_band'.",
    )

    @model_validator(mode="after")
    def _evidence_required_for_grounded_kinds(self) -> Claim:
        if self.kind in (ClaimKind.OBSERVED, ClaimKind.DERIVED) and not self.evidence:
            raise ValueError(
                f"Claim {self.id!r} is {self.kind.value} but cites no evidence. "
                "Observed and derived claims must be traceable; if it cannot be "
                "traced it is inferred, and should say so."
            )
        if self.kind is ClaimKind.DERIVED and self.derived_by is None:
            raise ValueError(
                f"Claim {self.id!r} is derived but does not name the rule that "
                "derived it. Unnamed rules cannot be audited."
            )
        if self.kind is ClaimKind.INFERRED and self.slot_numbers() and not self.evidence:
            raise ValueError(
                f"Claim {self.id!r} is inferred and carries numbers "
                f"{self.slot_numbers()} but cites no evidence. An inferred claim "
                "may be vague, but it may not be numerically vague -- that is "
                "how a hallucinated figure reaches a fisherman."
            )
        return self


class Hypothesis(Templated):
    """A causal proposal, plus the deterministic test that judged it.

    ``causal_explain`` only. The LLM proposes; code tests. Per the governing
    rule, an **untested hypothesis is dropped, not reported** -- which is why
    ``tested_by`` is required rather than optional. There is no way to build a
    hypothesis object that was never tested, so there is no way for one to
    reach the user.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    tested_by: str = Field(
        min_length=1,
        description="tool_call_id of the test. Untested hypotheses are dropped.",
    )
    supported: bool
    test_statistic: Range | None = None
    test_description: str | None = Field(
        default=None, description="What the test actually did, in one line."
    )
    evidence: list[str] = Field(min_length=1)


class OperationalGuidance(Templated):
    """An instruction, ordered by how much it matters."""

    model_config = ConfigDict(extra="forbid")

    priority: int = Field(ge=1)


# --------------------------------------------------------------------------
# Alternatives
# --------------------------------------------------------------------------


class AlternativeCost(BaseModel):
    """What taking the alternative costs.

    Requirement 6 of the ideal answer. "Palk Bay is calmer" is close to
    useless; "Palk Bay is calmer but 55 km further" is a decision a man can
    make. An alternative without a cost is advice without a price tag, so this
    field is required on :class:`Alternative`.
    """

    model_config = ConfigDict(extra="forbid")

    extra_distance_km: float = Field(ge=0.0)
    extra_steam_time_h: float | None = Field(default=None, ge=0.0)
    note: str | None = None


class Alternative(Templated):
    """A different option, with its cost and its own verdict."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    cost: AlternativeCost
    verdict: VerdictValue
    evidence: list[str] = Field(min_length=1)


# --------------------------------------------------------------------------
# Context, assumptions, confidence
# --------------------------------------------------------------------------


class ResolvedPlace(BaseModel):
    """A coordinate that a tool produced, never one an LLM produced."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    source: str = Field(min_length=1, description="e.g. 'landing_centres'.")
    resolved_by: str = Field(min_length=1, description="tool_call_id. Required.")


class SpatialContext(BaseModel):
    """Where this answer applies, and what was carried in from earlier turns."""

    model_config = ConfigDict(extra="forbid")

    origin: ResolvedPlace | None = None
    vessel_class: VesselClass | None = None
    aoi_bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="(west, south, east, north) in EPSG:4326."
    )
    inherited_from_turn: str | None = Field(
        default=None,
        description=(
            "Turn id this context came from. Every inherited field must also "
            "appear in assumptions[] -- the user sees what was assumed."
        ),
    )


class Assumption(BaseModel):
    """Something taken as given, surfaced so the user can correct it."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    field: str | None = None
    value: str | float | int | None = None
    source_turn: str | None = None
    confirmed_by_user: bool = False


class Confidence(BaseModel):
    """How much to trust this, overall and per claim.

    ``basis`` is required prose and is the field that makes the numbers
    defensible: "Open-Meteo marine forecast issued 6 h ago, 0.05 degree wave
    grid, afternoon build timing uncertain by +/-2 h". A confidence score
    without a stated basis is a number pretending to be an argument.

    Per-claim confidence exists because one overall figure would be a lie in
    both directions -- high for tomorrow morning, moderate for the timing of
    the afternoon build.
    """

    model_config = ConfigDict(extra="forbid")

    overall: float = Field(ge=0.0, le=1.0)
    by_claim: dict[str, float] = Field(default_factory=dict)
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bounds(self) -> Confidence:
        bad = {k: v for k, v in self.by_claim.items() if not 0.0 <= v <= 1.0}
        if bad:
            raise ValueError(f"by_claim values must be in [0, 1]; got {bad}.")
        return self


class Caveat(BaseModel):
    """A qualification, pointed at the specific claims it qualifies."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    applies_to: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Evidence and trace
# --------------------------------------------------------------------------


class EvidenceEntry(BaseModel):
    """One tool call, recorded so the verifier and the drawer can walk it.

    ``output_digest`` holds the numbers the answer is allowed to quote. The
    verifier checks claim slots against these values, so a number that is not
    in a digest cannot legally appear in a claim. Keeping a digest rather than
    the whole output keeps the object small enough to store per turn.

    The degraded-data fields are here rather than optional extras because
    cloud gaps are the single biggest data risk in this project. Carrying
    ``data_age_days`` next to every value is what makes
    ``confidence.basis`` honest instead of decorative.
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    output_digest: dict[str, Any] = Field(default_factory=dict)

    source: str = Field(min_length=1, description="Dataset or 'deterministic'.")
    retrieved_at: datetime | None = None
    issued_at: datetime | None = Field(
        default=None, description="Forecast issue time, distinct from retrieval."
    )
    native_resolution_deg: float | None = None

    data_age_days: float | None = Field(
        default=None, ge=0.0, description="Age of the underlying observation."
    )
    clear_pass_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Cloud-free fraction, satellite only."
    )
    composite_window_days: int | None = Field(
        default=None, ge=1, description="Set when a 3-day or 7-day composite was used."
    )

    status: Literal["ok", "degraded", "failed"] = "ok"
    error: str | None = None

    def numeric_values(self) -> list[float]:
        """Every number in the digest, flattened. The verifier's haystack."""
        out: list[float] = []

        def walk(node: Any) -> None:
            if isinstance(node, bool):
                return
            if isinstance(node, (int, float)):
                out.append(float(node))
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, (list, tuple)):
                for v in node:
                    walk(v)

        walk(self.output_digest)
        return out


class VisualLayer(BaseModel):
    """A map or chart layer spec. The frontend renders, it does not decide."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: Literal["point", "polygon", "line", "raster", "timeseries", "vector_field"]
    ref: str = Field(min_length=1, description="tool_call_id supplying the data.")
    threshold: float | None = Field(
        default=None, description="Draws the limit line on a timeseries."
    )
    label: str | None = None
    style_hint: str | None = None


class ReasoningStep(BaseModel):
    """One executed plan step. Populates the explainability drawer."""

    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    status: Literal["ok", "degraded", "failed", "skipped"]
    ms: int | None = Field(default=None, ge=0)
    note: str | None = None
    tool_call_id: str | None = None


# --------------------------------------------------------------------------
# The object itself
# --------------------------------------------------------------------------


class Recommendation(BaseModel):
    """The single response shape for all four query types and every failure mode.

    One shape, not four, so the frontend has one renderer and the verifier has
    one walk. The variants are expressed by which blocks are populated:

    * **verdict**   -- ``verdict`` set, ``drivers`` and ``window`` populated.
    * **causal**    -- ``verdict`` is None, ``hypotheses`` populated.
    * **degraded**  -- ``degraded`` True, some evidence entries ``failed``,
      verdict downgraded, caveats explaining what could not be checked.
    * **refusal**   -- ``refusal`` set, ``verdict`` None, no drivers.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    query_type: QueryType
    turn_id: str = Field(min_length=1)
    session_id: str | None = None
    generated_at: datetime | None = None

    verdict: Verdict | None = None
    refusal: Refusal | None = None
    headline: Templated

    claims: list[Claim] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    negative_findings: list[NegativeFinding] = Field(default_factory=list)
    operational_guidance: list[OperationalGuidance] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)

    window: Window | None = None
    spatial_context: SpatialContext | None = None

    assumptions: list[Assumption] = Field(default_factory=list)
    confidence: Confidence | None = None
    caveats: list[Caveat] = Field(default_factory=list)

    evidence: list[EvidenceEntry] = Field(default_factory=list)
    visual_layers: list[VisualLayer] = Field(default_factory=list)
    reasoning_trace: list[ReasoningStep] = Field(default_factory=list)

    degraded: bool = False
    degradation_notes: list[str] = Field(default_factory=list)

    # -- validators ------------------------------------------------------

    @model_validator(mode="after")
    def _check_verdict_and_refusal(self) -> Recommendation:
        if self.verdict is not None and self.refusal is not None:
            raise ValueError(
                "A recommendation cannot both reach a verdict and refuse. "
                "Populate exactly one."
            )
        if self.refusal is not None and (self.drivers or self.window):
            raise ValueError(
                "A refusal must not carry drivers or a window -- nothing was "
                "assessed, and rendering half an assessment implies it was."
            )
        if self.query_type is QueryType.CAUSAL_EXPLAIN and self.verdict is not None:
            raise ValueError(
                "causal_explain produces hypotheses, not a safety verdict. "
                "A causal query that returns a verdict has confused explaining "
                "with advising."
            )
        return self

    @model_validator(mode="after")
    def _check_evidence_links(self) -> Recommendation:
        """Rule 3: every evidence reference must resolve to a real tool call.

        Run at construction, so a dangling ``tool_call_id`` cannot be built.
        This is the structural half of the guarantee; ``orchestrator/verifier.py``
        does the numeric half.
        """
        known = {e.tool_call_id for e in self.evidence}
        if not known:
            # Fixtures and unit tests legitimately build fragments with no
            # evidence list attached. Nothing to check against.
            return self

        dangling: list[str] = []

        def check(refs: list[str], where: str) -> None:
            for ref in refs:
                if ref not in known:
                    dangling.append(f"{where} -> {ref}")

        for c in self.claims:
            check(c.evidence, f"claim[{c.id}]")
        for d in self.drivers:
            check(d.evidence, f"driver[{d.id}]")
        for h in self.hypotheses:
            check(h.evidence, f"hypothesis[{h.id}]")
            check([h.tested_by], f"hypothesis[{h.id}].tested_by")
        for a in self.alternatives:
            check(a.evidence, f"alternative[{a.id}]")
        for nf in self.negative_findings:
            check([nf.checked_by], f"negative_finding[{nf.id}].checked_by")
        for vl in self.visual_layers:
            check([vl.ref], f"visual_layer[{vl.id}]")
        if self.verdict is not None:
            check([self.verdict.computed_by], "verdict.computed_by")
        if self.window is not None:
            check([self.window.basis], "window.basis")
        if self.spatial_context and self.spatial_context.origin:
            check([self.spatial_context.origin.resolved_by], "spatial_context.origin")

        if dangling:
            raise ValueError(
                "Evidence references do not resolve to any tool call: "
                f"{dangling}. Every claim points at evidence and every evidence "
                "entry points at a tool_call_id -- that link is what makes the "
                "verifier real."
            )
        return self

    @model_validator(mode="after")
    def _check_confidence_keys(self) -> Recommendation:
        if self.confidence is None or not self.claims:
            return self
        claim_ids = {c.id for c in self.claims}
        unknown = set(self.confidence.by_claim) - claim_ids
        if unknown:
            raise ValueError(
                f"confidence.by_claim references unknown claim ids {sorted(unknown)}."
            )
        return self

    @model_validator(mode="after")
    def _degraded_cannot_be_go(self) -> Recommendation:
        """A missing safety input can never produce a ``go``.

        The rule from the ideal answer document. If the alert check failed we
        do not know whether a cyclone is active, and "probably fine" is not an
        answer anyone should act on. Degradation may lower a verdict and may
        never permit the top one.
        """
        failed = [e.tool_call_id for e in self.evidence if e.status == "failed"]
        if (
            self.query_type is QueryType.SAFETY_ASSESS
            and self.verdict is not None
            and self.verdict.value is VerdictValue.GO
            and (self.degraded or failed)
        ):
            raise ValueError(
                "Verdict is 'go' but the answer is degraded "
                f"(failed tool calls: {failed or 'none'}, degraded flag: "
                f"{self.degraded}). A safety check that did not complete cannot "
                "produce a clean go -- downgrade with Verdict.downgraded_to()."
            )
        return self

    @model_validator(mode="after")
    def _inherited_slots_are_declared(self) -> Recommendation:
        """Inherited context must be visible to the user as an assumption."""
        ctx = self.spatial_context
        if ctx is None or ctx.inherited_from_turn is None:
            return self
        if not any(a.source_turn == ctx.inherited_from_turn for a in self.assumptions):
            raise ValueError(
                f"spatial_context was inherited from turn "
                f"{ctx.inherited_from_turn!r} but no assumption declares it. "
                "Silently reusing a location or vessel class across turns is "
                "how a safety answer ends up describing the wrong boat."
            )
        return self

    # -- helpers ---------------------------------------------------------

    def evidence_by_id(self) -> dict[str, EvidenceEntry]:
        return {e.tool_call_id: e for e in self.evidence}

    def breaching_drivers(self) -> list[Driver]:
        return [d for d in self.drivers if d.breaching]

    def is_refusal(self) -> bool:
        return self.refusal is not None
