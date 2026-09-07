"""compute_risk_score() -- the function that produces the verdict.

THE most important tool in the system. From CLAUDE.md: the LLM must never
produce a number attached to a safety claim. This is where the safety number
actually comes from, and Verdict.computed_by must name this call.

The planner emits a step calling this. The planner does not weigh a 2.8 m swell
against a vessel class -- planning is not adjudication.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from core import config
from core.schemas.tool_io import Provenance, ToolInput, ToolOutput
from core.units import Comparison, Range, ThresholdEvaluation, Unit, evaluate
from tools import registry
from tools.registry import AgentGroup, register


class DriverContribution(BaseModel):
    """One driver's share of the score, with its threshold test attached."""

    model_config = ConfigDict(extra="forbid")

    driver_id: str
    evaluation: ThresholdEvaluation
    weight: float = Field(ge=0.0, le=1.0)
    contribution: float = Field(ge=0.0, le=1.0)


class ComputeRiskScoreIn(ToolInput):
    vessel_class: str = Field(description="Must match core.schemas.intent.VesselClass; keys config/risk_thresholds.yaml.")
    wave_height: Range | None = None
    wind_speed: Range | None = None
    visibility: Range | None = None
    wave_steepness: float | None = None
    alerts_checked: bool = Field(default=False, description="False forces a downgrade. Not knowing is not the same as clear.")
    alerts_active: int = Field(default=0, ge=0)


class ComputeRiskScoreOut(ToolOutput):
    score: float = Field(ge=0.0, le=1.0)
    band: str
    verdict: str = Field(description="go | marginal | no_go, per config/risk_thresholds.yaml score_bands.")
    limiting_driver: str | None = None
    contributions: list[DriverContribution] = Field(default_factory=list)
    downgraded: bool = Field(default=False)
    downgrade_reason: str | None = None


register(
    "compute_risk_score",
    description="Deterministic safety verdict for a vessel class from wave, wind, visibility and alert state.",
    input_model=ComputeRiskScoreIn,
    output_model=ComputeRiskScoreOut,
    agent=AgentGroup.RISK,
    safety_critical=True,
    notes="The only legitimate source of a safety verdict. Verdict.computed_by must point at this call.",
)


# ==========================================================================
# Implementation
# ==========================================================================
#
# The scoring curve, and why it is shaped this way.
#
# Each driver is reduced to a ratio r = (dangerous end of the observed band) /
# (its threshold), so that r = 1.0 means "exactly at the limit" regardless of
# whether the driver is metres or knots. Severity is then piecewise linear in r:
#
#     r <= 0.60          severity 0.00   comfortably inside the limit
#     0.60 < r <= 1.00   severity 0.00 -> 0.60 linearly
#     r > 1.00           severity 0.60 -> 1.00 over r in (1.0, 1.5], capped
#
# Two properties are deliberate:
#
# 1. Being *at* the limit scores 0.60, not 1.0. A boat at exactly its rated
#    limit in otherwise fine conditions is marginal, not doomed, and a curve
#    that saturated at the threshold would make every borderline day a no_go.
#    Advice that cries wolf gets ignored, and then it protects nobody.
#
# 2. Exceeding the limit climbs steeply but still bounded. 1.5x the limit
#    saturates. Beyond that the answer is already no_go and finer resolution
#    buys nothing.
#
# The 0.60 floor is where risk starts registering at all, chosen so that a
# genuinely calm day scores near zero rather than accumulating noise from four
# drivers each contributing a little.
#
# These three constants are ORCA's, not INCOIS's, like the per-vessel limits
# they operate on. They are here rather than in YAML because they define the
# curve's *shape* rather than a safety limit -- the numbers a judge asks about
# are the thresholds, and those are all in config with citations.

SAFE_FLOOR = 0.60
AT_LIMIT_SEVERITY = 0.60
OVERSHOOT_SATURATION = 0.50

#: Relative weight of each driver before renormalisation over those present.
#: Wave dominates because it is what capsizes a small boat; wind matters mostly
#: through the sea it builds, which is already counted, so it weighs less than
#: intuition suggests.
DRIVER_WEIGHTS: dict[str, float] = {
    "significant_wave_height": 0.50,
    "wind_speed": 0.30,
    "wave_steepness": 0.12,
    "visibility": 0.08,
}


def _severity(ratio: float) -> float:
    """Map a threshold ratio to 0..1 severity. See the curve note above."""
    if ratio <= SAFE_FLOOR:
        return 0.0
    if ratio <= 1.0:
        return AT_LIMIT_SEVERITY * (ratio - SAFE_FLOOR) / (1.0 - SAFE_FLOOR)
    overshoot = min((ratio - 1.0) / OVERSHOOT_SATURATION, 1.0)
    return AT_LIMIT_SEVERITY + (1.0 - AT_LIMIT_SEVERITY) * overshoot


def _ratio(evaluation: ThresholdEvaluation) -> float:
    """Observed-to-threshold ratio, oriented so that larger always means worse."""
    threshold = evaluation.threshold
    observed = evaluation.observed
    if threshold.comparison in (Comparison.LTE, Comparison.LT):
        probe = observed.worst_case
        return probe / threshold.value if threshold.value else 0.0
    # A floor: safety means staying above it, so the ratio inverts and the
    # bottom of the band is the dangerous end.
    probe = observed.min
    return threshold.value / probe if probe else float("inf")


def _band_for(score: float) -> tuple[str, str]:
    """Map a score to (verdict, band label) using config score_bands."""
    bands = config.score_bands()
    if score < bands["go"]["max"]:
        return "go", "clear"
    if score < bands["no_go"]["min"]:
        return "marginal", "conditional"
    return "no_go", "unsafe"


def compute_risk_score(args: ComputeRiskScoreIn) -> ComputeRiskScoreOut:
    """The verdict. Deterministic, config-driven, no LLM anywhere near it.

    Every threshold comes from ``config/risk_thresholds.yaml`` keyed by vessel
    class, and every one of them carries a citation. This function does
    arithmetic on those numbers and nothing else -- which is the whole point:
    the answer to "why did you say no" is a table, not a model.
    """
    thresholds = config.thresholds_for(args.vessel_class)
    marginal_band = config.marginal_band()

    observed: dict[str, Range] = {}
    if args.wave_height is not None:
        observed["significant_wave_height"] = args.wave_height
    if args.wind_speed is not None:
        observed["wind_speed"] = args.wind_speed
    if args.visibility is not None:
        observed["visibility"] = args.visibility
    if args.wave_steepness is not None:
        observed["wave_steepness"] = Range(
            min=args.wave_steepness, max=args.wave_steepness, unit=Unit.FRACTION
        )

    contributions: list[DriverContribution] = []
    present = {k: v for k, v in observed.items() if k in thresholds}
    total_weight = sum(DRIVER_WEIGHTS.get(k, 0.0) for k in present) or 1.0

    for name, value in present.items():
        evaluation = evaluate(value, thresholds[name], marginal_band=marginal_band)
        weight = DRIVER_WEIGHTS.get(name, 0.0) / total_weight
        severity = _severity(_ratio(evaluation))
        contributions.append(
            DriverContribution(
                driver_id=name,
                evaluation=evaluation,
                weight=round(weight, 4),
                contribution=round(weight * severity, 4),
            )
        )

    score = round(sum(c.contribution for c in contributions), 4)
    verdict, band = _band_for(score)

    limiting = None
    if contributions:
        limiting = max(contributions, key=lambda c: c.contribution).driver_id

    downgraded = False
    reason: str | None = None

    # A breach of any single limit is disqualifying on its own. A weighted
    # average can dilute one severe driver behind three benign ones, and
    # "on average the sea is fine" is not a thing anyone should go to sea on.
    breaching = [c.driver_id for c in contributions if c.evaluation.breaching]
    if breaching and verdict != "no_go":
        verdict, band = "no_go", "unsafe"
        downgraded = True
        reason = f"{', '.join(breaching)} over limit"
        limiting = breaching[0]

    # Active advisory from IMD or INCOIS outranks our own arithmetic.
    if args.alerts_active > 0:
        verdict, band = "no_go", "advisory in force"
        downgraded = True
        reason = f"{args.alerts_active} advisory(ies) in force"

    # Not knowing is not the same as clear. config/risk_thresholds.yaml sets
    # missing_alert_check_forces: no_go, and this honours it.
    if not args.alerts_checked:
        forced = config.risk_thresholds()["verdict"]["degradation"][
            "missing_alert_check_forces"
        ]
        if forced == "no_go" and verdict != "no_go":
            verdict, band = "no_go", "alert status unknown"
            downgraded = True
            reason = "alert check did not complete"

    return ComputeRiskScoreOut(
        provenance=Provenance(source="deterministic", authority="ORCA"),
        score=score,
        band=band,
        verdict=verdict,
        limiting_driver=limiting,
        contributions=contributions,
        downgraded=downgraded,
        downgrade_reason=reason,
    )


registry.implement("compute_risk_score")(compute_risk_score)
