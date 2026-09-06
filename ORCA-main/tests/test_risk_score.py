"""Tests for compute_risk_score() -- the function that produces the verdict.

This is the one place in ORCA where a safety call actually gets made, so these
tests are about behaviour a fisherman would feel, not about code coverage.

The organising question throughout: **would a person who can see the sea agree
with this answer?** A risk function that is defensible on paper but tells a
trawler skipper to stay in on a 2 m day is not a good risk function, because he
will stop reading it, and then it protects nobody.
"""

from __future__ import annotations

import pytest

from core import config
from core.units import Range, Unit
from fixtures.safety_assess_nagapattinam import _IDEAL_SCORE
from tools.risk.risk_score import (
    DRIVER_WEIGHTS,
    ComputeRiskScoreIn,
    compute_risk_score,
)


def score(vessel_class: str = "frp_9m", **kw):
    kw.setdefault("alerts_checked", True)
    return compute_risk_score(ComputeRiskScoreIn(vessel_class=vessel_class, **kw))


WAVE_22 = Range(min=1.8, max=2.2, unit=Unit.METRE)
WIND_20G25 = Range(min=15.0, max=20.0, unit=Unit.KNOT, qualifier="gusting 25", peak=25.0)
CALM = Range(min=0.5, max=0.8, unit=Unit.METRE)
LIGHT_WIND = Range(min=6.0, max=10.0, unit=Unit.KNOT)


# ==========================================================================
# The ideal answer scenario
# ==========================================================================


def test_ideal_scenario_produces_a_marginal_verdict():
    """The scenario the whole system was designed around.

    Nothing is breaching, and the answer is still not a clean go. That is the
    honest call for 2.2 m against a 2.5 m limit, and a system that said 'go'
    here would be wrong in a way a fisherman would notice.
    """
    out = score(wave_height=WAVE_22, wind_speed=WIND_20G25)
    assert out.verdict == "marginal"
    assert out.limiting_driver == "significant_wave_height"
    assert not out.downgraded
    assert not any(c.evaluation.breaching for c in out.contributions)


def test_ideal_scenario_still_scores_as_the_fixture_claims():
    """Anti-drift guard between the function and the ideal answer fixture.

    The fixture's score is not hand-picked -- it is whatever this function
    returns. If the scoring curve is retuned, this fails and the fixture, the
    ideal-answer document and the verifier's tool log all have to be updated
    together, which is exactly the coupling we want to be loud.
    """
    out = score(wave_height=WAVE_22, wind_speed=WIND_20G25)
    assert out.score == pytest.approx(_IDEAL_SCORE, abs=1e-6)


# ==========================================================================
# The behaviour that makes it useful: same sea, different boats
# ==========================================================================


def test_the_same_sea_gives_three_different_answers_by_vessel_class():
    """The single most convincing thing this function does.

    2.2 m is a bad day for a kattumaram, a marginal one for a 9 m FRP boat,
    and an ordinary working day for a mechanised trawler. One number for the
    whole coast could not express that, which is precisely why INCOIS's
    district-scale alert is not a substitute for a per-boat answer.
    """
    verdicts = {
        vc: score(vc, wave_height=WAVE_22).verdict
        for vc in ("kattumaram", "frp_9m", "mechanised_trawler")
    }
    assert verdicts == {
        "kattumaram": "no_go",
        "frp_9m": "marginal",
        "mechanised_trawler": "go",
    }


def test_scores_are_ordered_by_vessel_capability():
    scores = [
        score(vc, wave_height=WAVE_22).score
        for vc in ("kattumaram", "frp_9m", "mechanised_trawler")
    ]
    assert scores == sorted(scores, reverse=True)


# ==========================================================================
# The curve
# ==========================================================================


def test_a_genuinely_calm_day_scores_near_zero():
    """Noise from several benign drivers must not accumulate into a warning."""
    out = score(wave_height=CALM, wind_speed=LIGHT_WIND)
    assert out.score == 0.0
    assert out.verdict == "go"


def test_being_exactly_at_the_limit_is_marginal_not_catastrophic():
    """A boat at its rated limit in otherwise fine conditions is marginal.

    A curve that saturated at the threshold would make every borderline day a
    no_go, and advice that cries wolf gets switched off.
    """
    at_limit = Range(min=2.4, max=2.5, unit=Unit.METRE)  # exactly the frp_9m limit
    out = score(wave_height=at_limit)
    assert not out.contributions[0].evaluation.breaching
    assert out.verdict == "marginal"


def test_exceeding_a_limit_forces_no_go_regardless_of_the_average():
    """A single breach is disqualifying; a weighted mean must not dilute it.

    Without this rule, one severe driver can hide behind three benign ones and
    the answer becomes 'on average the sea is fine', which is not a thing
    anyone should go to sea on.
    """
    out = score(
        wave_height=Range(min=2.9, max=3.2, unit=Unit.METRE),  # over 2.5
        wind_speed=LIGHT_WIND,
        visibility=Range(min=20.0, max=25.0, unit=Unit.KILOMETRE),
    )
    assert out.verdict == "no_go"
    assert out.downgraded
    assert "significant_wave_height" in out.downgrade_reason


def test_score_increases_monotonically_with_wave_height():
    heights = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    scores = [
        score(wave_height=Range(min=h - 0.2, max=h, unit=Unit.METRE)).score
        for h in heights
    ]
    assert scores == sorted(scores), scores


def test_score_is_bounded():
    out = score(wave_height=Range(min=9.0, max=12.0, unit=Unit.METRE))
    assert 0.0 <= out.score <= 1.0


# ==========================================================================
# Gusts and threshold direction
# ==========================================================================


def test_the_gust_is_thresholded_not_the_mean():
    """Gusts capsize boats; averages do not.

    Same sustained wind, different gust. The one gusting over the limit must
    score worse, or Range.peak is decorative.
    """
    steady = Range(min=15.0, max=20.0, unit=Unit.KNOT)
    gusty = Range(min=15.0, max=20.0, unit=Unit.KNOT, peak=34.0)
    assert score(wind_speed=gusty).score > score(wind_speed=steady).score
    assert score(wind_speed=gusty).verdict == "no_go"


def test_visibility_is_a_floor_not_a_ceiling():
    """Regression test for a real bug.

    Visibility is safe while ABOVE its threshold, so the dangerous end of the
    band is the minimum. An earlier version thresholded the maximum for every
    driver, which tested 1-1.5 km visibility using 1.5 -- the most reassuring
    number in the band rather than the most dangerous one, and worse, still
    the wrong end.
    """
    poor = Range(min=1.0, max=1.5, unit=Unit.KILOMETRE)  # floor is 2.0 km
    out = score(wave_height=CALM, visibility=poor)
    vis = next(c for c in out.contributions if c.driver_id == "visibility")
    assert vis.evaluation.breaching
    assert out.verdict == "no_go"

    good = Range(min=18.0, max=25.0, unit=Unit.KILOMETRE)
    assert not score(wave_height=CALM, visibility=good).verdict == "no_go"


# ==========================================================================
# Alerts: the rules that outrank the arithmetic
# ==========================================================================


def test_an_unchecked_alert_status_forces_no_go_even_on_a_calm_day():
    """Not knowing is not the same as clear.

    The sea could be glass; if the cyclone check did not complete we do not
    know whether a cyclone is coming, and 'probably fine' is not something to
    put a boat in the water on. Honours
    risk_thresholds.yaml verdict.degradation.missing_alert_check_forces.
    """
    out = score(wave_height=CALM, wind_speed=LIGHT_WIND, alerts_checked=False)
    assert out.verdict == "no_go"
    assert out.downgraded
    assert "alert check" in out.downgrade_reason
    # The arithmetic itself still says calm -- the downgrade is the reason,
    # and the drawer must be able to show that distinction.
    assert out.score == 0.0


def test_an_active_advisory_outranks_our_own_arithmetic():
    """IMD and INCOIS outrank us. We corroborate them; we do not overrule them."""
    out = score(wave_height=CALM, wind_speed=LIGHT_WIND, alerts_active=1)
    assert out.verdict == "no_go"
    assert "advisory" in out.downgrade_reason


# ==========================================================================
# Config coupling
# ==========================================================================


def test_an_unknown_vessel_class_raises_rather_than_guessing():
    """Scoring a boat we have no limits for is worse than refusing to score it."""
    with pytest.raises(KeyError, match="No thresholds configured"):
        score("submarine", wave_height=CALM)


def test_every_weighted_driver_exists_in_the_config():
    """A weight for a driver no vessel has thresholds for is dead code that
    silently changes the normalisation for every other driver."""
    configured: set[str] = set()
    for spec in config.risk_thresholds()["vessel_classes"].values():
        configured.update(spec["limits"])
    assert set(DRIVER_WEIGHTS) <= configured, set(DRIVER_WEIGHTS) - configured


def test_thresholds_come_from_config_not_from_the_code():
    """The citation shown in the UI must resolve to the config entry."""
    out = score(wave_height=WAVE_22)
    threshold = out.contributions[0].evaluation.threshold
    assert threshold.source == "risk_thresholds.yaml#significant_wave_height.frp_9m"
    assert threshold.value == 2.5


def test_weights_renormalise_over_the_drivers_actually_present():
    """A missing driver must not silently shrink the total score.

    Wave alone at a given severity should score the same as wave alone would
    inside a fuller assessment, or an answer built from fewer inputs would
    look safer merely for being less informed.
    """
    out = score(wave_height=WAVE_22)
    assert sum(c.weight for c in out.contributions) == pytest.approx(1.0)

    out2 = score(wave_height=WAVE_22, wind_speed=WIND_20G25)
    assert sum(c.weight for c in out2.contributions) == pytest.approx(1.0)


def test_output_is_provenanced_as_deterministic():
    out = score(wave_height=WAVE_22)
    assert out.provenance.source == "deterministic"


def test_score_appears_in_numeric_values_for_the_verifier():
    """The verifier finds the verdict score by flattening tool output."""
    out = score(wave_height=WAVE_22, wind_speed=WIND_20G25)
    assert out.score in out.numeric_values()
