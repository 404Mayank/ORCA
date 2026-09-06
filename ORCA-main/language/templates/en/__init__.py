"""English rendering of a recommendation object.

Takes the typed object and produces the paragraph a fisherman reads. Every
number it prints comes from a slot that the verifier has already checked
against the tool call log -- this layer formats, it never computes.

The shape of the output is set by docs/ideal_answers/safety_assess.md: verdict
first, numbers always paired with the threshold they are judged against, bad
news specific about WHEN, and an honest footer.
"""

from __future__ import annotations

from core.schemas.recommendation import Recommendation, VerdictValue

__all__ = ["render", "render_headline"]

_VERDICT_LINE = {
    VerdictValue.GO: "Safe to go out.",
    VerdictValue.MARGINAL: "Marginal - you can go, but watch the time.",
    VerdictValue.NO_GO: "Do not go out.",
}


#: Units that read badly in a sentence. A steepness of 0.055 is a ratio; saying
#: "the 0.055 fraction limit" is worse than saying nothing.
_SILENT_UNITS = {"fraction", "index", "count"}


def _driver_sentence(driver) -> str:
    """One driver, always paired with the limit it is judged against.

    The phrasing follows the threshold's DIRECTION, which is not cosmetic.
    Wave height and wind are ceilings -- safe while under. Visibility is a
    floor -- safe while above. Describing 24 km of visibility as "under the
    2 km limit" is not just clumsy, it tells a fisherman the opposite of what
    the check found.
    """
    from core.units import Comparison

    evaluation = driver.evaluation
    threshold = evaluation.threshold
    is_ceiling = threshold.comparison in (Comparison.LTE, Comparison.LT)

    if is_ceiling:
        state = "over the" if evaluation.breaching else "under the"
        noun = "limit"
    else:
        state = "below the" if evaluation.breaching else "above the"
        noun = "minimum"

    unit = "" if threshold.unit.value in _SILENT_UNITS else f" {threshold.unit.value}"
    return f"{driver.render()}, {state} {threshold.value:g}{unit} {noun}."


def render_headline(rec: Recommendation) -> str:
    if rec.refusal is not None:
        return rec.headline.render()
    if rec.verdict is None:
        return rec.headline.render()
    return _VERDICT_LINE[rec.verdict.value]


def render(rec: Recommendation) -> str:
    """The full answer, as plain text."""
    lines: list[str] = [render_headline(rec).upper() if rec.verdict else render_headline(rec)]

    if rec.refusal is not None:
        if rec.refusal.supported_region:
            lines.append(f"Covered area: {rec.refusal.supported_region}")
        return "\n\n".join(lines)

    # Drivers, each paired with the limit it is judged against. Never a bare
    # number -- "2.2 m" is a fact, "2.2 m against a 2.5 m limit" is an argument.
    if rec.drivers:
        lines.append(" ".join(_driver_sentence(d) for d in rec.drivers))

    # When it changes, and how fast.
    for driver in rec.drivers:
        trajectory = driver.trajectory
        if trajectory and trajectory.breaches_at:
            sentence = (
                f"It does not stay that way: conditions cross the limit at "
                f"{trajectory.breaches_at.strftime('%H:%M')}."
            )
            if trajectory.peak_rate_window:
                start, end = trajectory.peak_rate_window
                sentence += (
                    f" Most of the build happens between "
                    f"{start.strftime('%H:%M')} and {end.strftime('%H:%M')}."
                )
            lines.append(sentence)
            break

    if rec.window is not None:
        lines.append(
            f"Your window is {rec.window.opens.strftime('%H:%M')} to "
            f"{rec.window.turn_back.strftime('%H:%M')}. Start back by "
            f"{rec.window.turn_back.strftime('%H:%M')} to be ashore by "
            f"{rec.window.ashore_by.strftime('%H:%M')}."
        )

    # Hypotheses, supported first. This block was missing entirely, so a
    # causal answer rendered as a headline and a footer with nothing in
    # between -- and the narrator, given nothing to work from, drifted into
    # generic safety advice. Every hypothesis here has already been through a
    # deterministic test; "not supported" is a result worth reading, because
    # ruling something out is an answer.
    if rec.hypotheses:
        for hypothesis in sorted(rec.hypotheses, key=lambda h: not h.supported):
            mark = "Supported:" if hypothesis.supported else "Not supported:"
            lines.append(f"{mark} {hypothesis.render()}")
        untested = (
            "Explanations that could not be tested against data were dropped "
            "rather than listed."
        )
        if untested not in " ".join(c.text for c in rec.caveats):
            lines.append(untested)

    if rec.negative_findings:
        lines.append(" ".join(f.render() for f in rec.negative_findings))

    for guidance in sorted(rec.operational_guidance, key=lambda g: g.priority):
        lines.append(guidance.render())

    for alternative in rec.alternatives:
        cost = alternative.cost
        lines.append(
            f"{alternative.render()} It is about {cost.extra_distance_km:g} km "
            "further to steam."
        )

    # The honest footer. Never buried, never omitted.
    if rec.confidence is not None:
        lines.append(f"Based on: {rec.confidence.basis}")
    if rec.caveats:
        lines.append(" ".join(c.text for c in rec.caveats))

    return "\n\n".join(lines)
