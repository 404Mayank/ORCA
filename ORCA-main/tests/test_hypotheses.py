"""Causal hypotheses: the LLM proposes, code tests, untestable ones are dropped.

The rule under test is CLAUDE.md's, verbatim: *the LLM proposes hypotheses,
deterministic code tests them, and untested hypotheses are DROPPED, not
reported.* Every test here is really asking one of two questions -- can a model
change an outcome (it must not), and does an untestable proposal disappear (it
must).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from agents.hypotheses import ANOMALY_SIGMA, TESTS, propose
from core.provenance import ToolCallLog
from core.schemas.tool_io import Provenance
from core.units import Range, Unit
from orchestrator.executor import ExecutionResult
from orchestrator.llm.client import LLMResult
from tools.ocean.chl_anomaly import ChlAnomalyOut
from tools.ocean.thermal_front import ThermalFrontOut


def _result(*calls) -> ExecutionResult:
    log = ToolCallLog(turn_id="t_h")
    outputs, ids = {}, {}
    for step_id, tool, output in calls:
        record = log.record(
            tool=tool, step_id=step_id, args={}, output=output,
            started_at=datetime.now(timezone.utc), duration_ms=1,
        )
        outputs[step_id] = output
        ids[step_id] = record.tool_call_id
    return ExecutionResult(log=log, outputs=outputs, call_ids=ids)


def _chl(sigma: float | None):
    return ChlAnomalyOut(
        provenance=Provenance(source="viirs", authority="NOAA"),
        concentration=Range(min=0.4, max=1.2, unit=Unit.MILLIGRAM_PER_CUBIC_METRE),
        anomaly_sigma=sigma,
        climatology_mean=1.0 if sigma is not None else None,
        month=9,
    )


def _fronts(max_gradient: float | None, count: int = 0):
    return ThermalFrontOut(
        provenance=Provenance(source="mur", authority="NOAA"),
        fronts=[],
        max_gradient_deg_c_per_km=max_gradient,
    )


def _reply(*test_ids: str) -> LLMResult:
    return LLMResult(
        ok=True,
        text=json.dumps({
            "hypotheses": [
                {"test_id": t, "statement": f"statement for {t}"} for t in test_ids
            ]
        }),
        provider="stub",
        model="stub",
    )


# ==========================================================================
# The rule
# ==========================================================================


def test_a_proposal_with_no_matching_test_is_dropped_entirely():
    """The headline rule. An untestable explanation printed beside measured
    ones reads as a finding, and it is not one."""
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch("agents.hypotheses.llm.complete", return_value=_reply("the_moon_was_wrong")):
        proposals, error = propose(result, "Cuddalore")
    assert error is None
    assert proposals == []


def test_a_proposal_whose_tool_did_not_run_is_dropped():
    """The test exists, but there is no measurement to run it against."""
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch("agents.hypotheses.llm.complete", return_value=_reply("weak_frontal_structure")):
        proposals, _ = propose(result, "Cuddalore")
    assert proposals == []


def test_a_proposal_whose_tool_ran_but_produced_nothing_testable_is_dropped():
    """Chlorophyll with no baseline gives no anomaly, so nothing can be said
    about whether productivity is unusual."""
    result = _result(("s2", "chl_anomaly", _chl(None)))
    with patch("agents.hypotheses.llm.complete", return_value=_reply("low_productivity")):
        proposals, _ = propose(result, "Cuddalore")
    assert proposals == []


def test_the_model_cannot_decide_the_outcome():
    """It proposes the question. The measurement answers it.

    The same proposal, against opposite data, must come back with opposite
    outcomes -- otherwise the model's wording is doing the deciding.
    """
    low = _result(("s2", "chl_anomaly", _chl(-3.0)))
    normal = _result(("s2", "chl_anomaly", _chl(0.2)))

    with patch("agents.hypotheses.llm.complete", return_value=_reply("low_productivity")):
        supported, _ = propose(low, "Cuddalore")
        unsupported, _ = propose(normal, "Cuddalore")

    assert supported[0].outcome.supported is True
    assert unsupported[0].outcome.supported is False
    assert supported[0].outcome.statistic == -3.0


def test_a_proposal_carries_the_tool_call_it_was_tested_by():
    """Without this the hypothesis could not cite its evidence, and the
    explainability drawer would show a claim resting on nothing."""
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch("agents.hypotheses.llm.complete", return_value=_reply("low_productivity")):
        proposals, _ = propose(result, "Cuddalore")
    assert proposals[0].tool_call_id in result.tool_call_log


def test_numbers_are_stripped_from_a_proposed_statement():
    """A hypothesis is shown to the user. It may not carry a model's figure."""
    reply = LLMResult(
        ok=True,
        text=json.dumps({"hypotheses": [
            {"test_id": "low_productivity", "statement": "chlorophyll fell to 0.4 mg/m3"}
        ]}),
        provider="stub", model="stub",
    )
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch("agents.hypotheses.llm.complete", return_value=reply):
        proposals, _ = propose(result, "Cuddalore")
    assert "0.4" not in proposals[0].statement


def test_no_llm_means_no_proposals_and_a_reported_reason():
    """The built-in pair still runs; this only stops the search widening."""
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch(
        "agents.hypotheses.llm.complete",
        return_value=LLMResult(ok=False, error="rate limited"),
    ):
        proposals, error = propose(result, "Cuddalore")
    assert proposals == []
    assert "rate limited" in error


def test_duplicate_proposals_are_collapsed():
    result = _result(("s2", "chl_anomaly", _chl(-3.0)))
    with patch(
        "agents.hypotheses.llm.complete",
        return_value=_reply("low_productivity", "low_productivity"),
    ):
        proposals, _ = propose(result, "Cuddalore")
    assert len(proposals) == 1


# ==========================================================================
# The tests themselves
# ==========================================================================


@pytest.mark.parametrize(
    "sigma,expected",
    [(-3.0, True), (-ANOMALY_SIGMA, True), (-1.0, False), (0.0, False), (3.0, False)],
)
def test_low_productivity_fires_only_on_a_low_anomaly(sigma, expected):
    outcome = TESTS["low_productivity"].run(_chl(sigma))
    assert outcome.supported is expected


@pytest.mark.parametrize("sigma,expected", [(3.0, True), (1.0, False), (-3.0, False)])
def test_bloom_fires_only_on_a_high_anomaly(sigma, expected):
    """The mirror of the above. Near this coast a high reading can mean a
    turbid river plume rather than a productive sea, which is why both
    directions are worth testing."""
    outcome = TESTS["bloom_or_turbidity"].run(_chl(sigma))
    assert outcome.supported is expected


def test_no_fronts_fires_when_the_gradient_is_below_the_floor():
    assert TESTS["weak_frontal_structure"].run(_fronts(0.02)).supported is True
    assert TESTS["weak_frontal_structure"].run(_fronts(0.30)).supported is False


def test_every_test_names_a_real_tool():
    from tools import registry

    for test in TESTS.values():
        assert registry.has(test.tool), test.id
        assert registry.get(test.tool).implemented, test.id


def test_every_test_blurb_describes_the_test_not_the_answer():
    """The model chooses tests, not conclusions. A blurb that asserted an
    outcome would be putting words in the measurement's mouth."""
    for test in TESTS.values():
        assert "Decided by" in test.blurb or "counts as" in test.blurb, test.id
