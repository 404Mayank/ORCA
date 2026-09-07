"""Regression tests for the wind-gust display bug.

Root cause: compute_risk_score thresholds Range.worst_case (the gust peak
when present), but the driver table rendered sustained min-max against the
limit -- "wind 0.1-10.3 kn vs 15 kn: Over limit" reads as a lie because
10.3 < 15. The displayed numbers must include the peak that drove the
evaluation, and breach wording must be true of what is shown.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agents.intent_planner_agent import _force_clarification, parse_planner_json
from agents.synthesis_agent import _drivers
from core.schemas.intent import Intent, QueryType
from core.units import Comparison, Range, Threshold, Unit, evaluate
from language.templates.en import _driver_sentence
from tools.risk.risk_score import ComputeRiskScoreIn, compute_risk_score

WIND_LIMIT = Threshold(
    value=15.0, unit=Unit.KNOT, comparison=Comparison.LTE,
    source="risk_thresholds.yaml#wind_speed.kattumaram",
)
VESSEL = "kattumaram"


def _wind_driver(sustained_min, sustained_max, peak):
    observed = Range(min=sustained_min, max=sustained_max, unit=Unit.KNOT, peak=peak)
    evaluation = evaluate(observed, WIND_LIMIT)
    risk = compute_risk_score(
        ComputeRiskScoreIn(
            vessel_class=VESSEL, wind_speed=observed,
            alerts_checked=True, alerts_active=0,
        )
    )
    result = SimpleNamespace(
        output_for=lambda _name: None, call_id_for=lambda _name: "s3-test"
    )
    drivers = _drivers(result, risk, 999.0)
    assert len(drivers) == 1
    assert drivers[0].evaluation.breaching == evaluation.breaching
    return drivers[0]


def test_peak_breach_display_includes_peak_number():
    """Sustained 5-10.3 kn under a 15 kn limit, gust 25.9 over it."""
    driver = _wind_driver(5.0, 10.3, 25.9)
    assert driver.evaluation.breaching
    assert "25.9" in driver.render()


def test_peak_breach_wording_references_the_gust():
    driver = _wind_driver(5.0, 10.3, 25.9)
    sentence = _driver_sentence(driver)
    assert "25.9" in sentence
    assert "gust" in sentence.lower()


def test_no_peak_driver_renders_exactly_as_before():
    driver = _wind_driver(5.0, 10.3, None)
    assert not driver.evaluation.breaching
    assert driver.render() == "Wind 5.0-10.3 kn"
    assert _driver_sentence(driver) == "Wind 5.0-10.3 kn, under the 15 kn limit."


def _clarification_json(options):
    return json.dumps(
        {
            "intent": {
                "query_type": "safety_assess",
                "raw_query": "is it safe to go out?",
                "spatial_reference": {"name": "Nagapattinam"},
                "vessel_class": None,
                "missing_slots": ["vessel_class"],
            },
            "state": "clarification",
            "clarification": {
                "missing_slots": ["vessel_class"],
                "question_template": "What kind of boat is it?",
                "options": options,
            },
        }
    )


def test_model_returned_duplicate_options_are_deduped_order_preserving():
    out = parse_planner_json(
        _clarification_json(["kattumaram", "frp_9m", "mechanised_trawler", "kattumaram"]),
        "is it safe to go out?",
    )
    assert out.clarification is not None
    assert out.clarification.options == ["kattumaram", "frp_9m", "mechanised_trawler"]


def test_forced_clarification_options_have_no_duplicates():
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS, raw_query="is it safe?",
        missing_slots=["vessel_class"],
    )
    out = _force_clarification(intent, ["vessel_class"])
    options = out.clarification.options
    assert len(options) == len(dict.fromkeys(options))
