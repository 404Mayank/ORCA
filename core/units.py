"""Units, ranges and thresholds.

The lowest layer in ORCA. This module imports from nothing inside the project
and everything else may import from it.

Two ideas live here, and both exist because of the governing rule in CLAUDE.md:

1.  **Every measured value is a range, never a scalar.** Real marine data is
    "15-20 knots gusting 25", not "17.5 knots". A scalar is a lie about
    precision we do not have, and once it reaches a fisherman it reads as
    certainty. :class:`Range` makes the honest form the easy form.

2.  **Comparing a value to a threshold is arithmetic, not judgement.** The LLM
    never decides whether 2.8 m is too rough. :func:`evaluate` decides, and the
    LLM reports what it decided. Every threshold carries a ``source`` pointing
    at the line in ``config/risk_thresholds.yaml`` that justifies it, because
    that is the file we open when a judge asks why 2.5 m.

Half the failure modes in a project like this are unit confusion, so units are
a closed enum rather than free text. If a unit is not in :class:`Unit`, it is
not in the system.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Unit",
    "Comparison",
    "Range",
    "Threshold",
    "ThresholdEvaluation",
    "evaluate",
    "KNOTS_PER_MS",
    "ms_to_knots",
    "knots_to_ms",
]


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


class Unit(str, Enum):
    """Every unit the system is allowed to speak.

    Deliberately small. Adding one is a decision, not a convenience, because
    each new unit is a new opportunity to confuse m/s with knots.
    """

    # Distance and depth
    METRE = "m"
    KILOMETRE = "km"
    NAUTICAL_MILE = "nmi"

    # Speed
    KNOT = "kn"
    METRE_PER_SECOND = "m/s"

    # Time
    HOUR = "h"
    SECOND = "s"
    DAY = "d"

    # Direction and position
    DEGREE_TRUE = "degT"
    DEGREE = "deg"

    # Ocean and atmosphere
    DEGREE_CELSIUS = "degC"
    MILLIGRAM_PER_CUBIC_METRE = "mg/m3"
    DEGREE_CELSIUS_PER_KM = "degC/km"

    # Dimensionless
    FRACTION = "fraction"
    COUNT = "count"
    INDEX = "index"


#: Exact conversion factor. One knot is one nautical mile (1852 m) per hour.
KNOTS_PER_MS: float = 3600.0 / 1852.0


def ms_to_knots(value: float) -> float:
    """Metres per second to knots."""
    return value * KNOTS_PER_MS


def knots_to_ms(value: float) -> float:
    """Knots to metres per second."""
    return value / KNOTS_PER_MS


# --------------------------------------------------------------------------
# Range
# --------------------------------------------------------------------------


class Range(BaseModel):
    """A measured or forecast value, expressed as the band it actually is.

    ``qualifier`` carries the part of a marine forecast that does not fit in a
    band -- "gusting 25", "occasionally 3 m", "in squalls". It is free text and
    is *never* parsed for a number by anything downstream. If a qualifier
    contains an operationally important number, that number belongs in its own
    field, not buried in prose. See :attr:`Range.peak` for the gust case.

    A single known value is expressed as ``Range(min=x, max=x, ...)``, not as a
    scalar. Keeping one representation means the narration layer has one code
    path instead of two.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min: float
    max: float
    unit: Unit
    qualifier: str | None = None
    peak: float | None = Field(
        default=None,
        description=(
            "The operationally significant excursion above `max`: a wind gust, "
            "a rogue wave allowance. Kept as a number, not text, because the "
            "risk function must be able to threshold against it."
        ),
    )

    @model_validator(mode="after")
    def _check_ordering(self) -> Range:
        if self.min > self.max:
            raise ValueError(
                f"Range min ({self.min}) exceeds max ({self.max}). "
                "A backwards range usually means two sources were mixed up."
            )
        if self.peak is not None and self.peak < self.max:
            raise ValueError(
                f"Range peak ({self.peak}) is below max ({self.max}). "
                "A peak that is not a peak hides the worst case from the "
                "risk function."
            )
        return self

    @property
    def midpoint(self) -> float:
        """Centre of the band.

        Use for display ordering and sorting only. Never threshold against
        this -- thresholding uses :attr:`worst_case`, because a band whose
        midpoint is safe can still have a top end that is not.
        """
        return (self.min + self.max) / 2.0

    @property
    def worst_case(self) -> float:
        """The dangerous end of the band for an upper limit (LTE/LT).

        The top of the band, or the peak if one is recorded. Marine risk is
        asymmetric: being wrong about the calm end costs a wasted trip, being
        wrong about the rough end costs a boat.

        For a *lower* limit -- visibility, depth under keel, where safety means
        staying above a floor -- the dangerous end is :attr:`min` instead.
        :func:`evaluate` picks the right end from the threshold's direction;
        do not reach for this property directly when thresholding.
        """
        return self.peak if self.peak is not None else self.max

    @property
    def spread(self) -> float:
        """Width of the band. A proxy for forecast uncertainty."""
        return self.max - self.min

    def is_point(self) -> bool:
        """True if the band has collapsed to a single value."""
        return self.min == self.max


# --------------------------------------------------------------------------
# Threshold
# --------------------------------------------------------------------------


class Comparison(str, Enum):
    """Direction of a threshold test.

    Read as "the observed value is SAFE while it is ``<comparison>`` the
    threshold". ``LTE`` means safe while at or below -- wave height, wind
    speed. ``GTE`` means safe while at or above -- visibility, depth under
    keel.
    """

    LTE = "lte"
    LT = "lt"
    GTE = "gte"
    GT = "gt"


class Threshold(BaseModel):
    """A limit a value is judged against, and the citation that justifies it.

    ``source`` is required and is not decoration. It is a pointer of the form
    ``risk_thresholds.yaml#wave_height.frp_9m`` into the config file, which in
    turn carries a comment citing INCOIS small-vessel alert criteria. The chain
    from a number on screen to a published criterion has to be walkable, or the
    number is just an opinion with a decimal point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    unit: Unit
    comparison: Comparison
    source: str = Field(
        min_length=1,
        description=(
            "Citation pointer, e.g. 'risk_thresholds.yaml#wave_height.frp_9m'. "
            "An uncited threshold is not admissible."
        ),
    )
    label: str | None = None

    def is_safe(self, value: float) -> bool:
        """Test a bare number against this threshold."""
        match self.comparison:
            case Comparison.LTE:
                return value <= self.value
            case Comparison.LT:
                return value < self.value
            case Comparison.GTE:
                return value >= self.value
            case Comparison.GT:
                return value > self.value
        raise AssertionError(f"unhandled comparison {self.comparison}")


class ThresholdEvaluation(BaseModel):
    """The result of comparing a :class:`Range` to a :class:`Threshold`.

    This object is the whole point of the module. It is what a driver in the
    recommendation carries, and it holds the observed value and the limit
    together so the narration layer physically cannot mention one without the
    other. "Waves 1.8-2.2 m" is a number. "Waves 1.8-2.2 m against a 2.5 m
    limit" is an argument.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed: Range
    threshold: Threshold
    breaching: bool
    margin: float = Field(
        description=(
            "Signed distance from the worst case to the limit, in the "
            "threshold's unit. Positive means headroom, negative means the "
            "limit is exceeded and by how much."
        )
    )
    margin_fraction: float | None = Field(
        default=None,
        description=(
            "Margin as a fraction of the threshold value. Lets the risk "
            "function compare a wave breach against a wind breach on one "
            "scale. None when the threshold is zero."
        ),
    )
    marginal: bool = Field(
        default=False,
        description=(
            "Not breaching, but inside the caution band near the limit. This "
            "is what produces a 'marginal' verdict rather than a flat go, and "
            "it is the case the ideal answer in docs/ideal_answers/ is built "
            "around."
        ),
    )


#: Fraction of a threshold within which a non-breaching value is called
#: marginal. 0.15 means a 2.5 m limit starts feeling marginal at 2.125 m.
#: Overridable per driver from config; this is the fallback.
DEFAULT_MARGINAL_BAND: float = 0.15


def evaluate(
    observed: Range,
    threshold: Threshold,
    marginal_band: float = DEFAULT_MARGINAL_BAND,
) -> ThresholdEvaluation:
    """Compare an observed range to a threshold. Deterministic. No LLM.

    Units must match exactly -- there is no implicit conversion, because a
    silent conversion is exactly the bug that unit confusion produces. Convert
    at the tool boundary with :func:`ms_to_knots` and friends, where the
    conversion is visible in a diff.

    The comparison uses :attr:`Range.worst_case`, not the midpoint. See that
    property for why.
    """
    if observed.unit != threshold.unit:
        raise ValueError(
            f"Unit mismatch: observed is {observed.unit.value}, threshold is "
            f"{threshold.unit.value}. Convert explicitly at the tool boundary; "
            "this function will not guess."
        )

    # Which end of the band is the dangerous end depends on which way the
    # threshold runs, and getting this wrong is silent.
    #
    #   LTE/LT  -- safe while below. Wave height, wind speed. The top of the
    #              band (or the gust) is what can hurt you.
    #   GTE/GT  -- safe while above. Visibility, depth under keel. The BOTTOM
    #              of the band is what can hurt you.
    #
    # Using Range.worst_case unconditionally would test visibility of 6-8 km
    # against its floor using 8, which is the most reassuring number in the
    # band rather than the most dangerous one.
    if threshold.comparison in (Comparison.LTE, Comparison.LT):
        probe = observed.worst_case
    else:
        probe = observed.min

    safe = threshold.is_safe(probe)

    # Margin is always signed so that positive means headroom, regardless of
    # which direction the threshold runs.
    if threshold.comparison in (Comparison.LTE, Comparison.LT):
        margin = threshold.value - probe
    else:
        margin = probe - threshold.value

    margin_fraction = (
        margin / abs(threshold.value) if threshold.value != 0 else None
    )

    is_marginal = (
        safe
        and margin_fraction is not None
        and 0.0 <= margin_fraction <= marginal_band
    )

    return ThresholdEvaluation(
        observed=observed,
        threshold=threshold,
        breaching=not safe,
        margin=margin,
        margin_fraction=margin_fraction,
        marginal=is_marginal,
    )
