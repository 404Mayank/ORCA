"""Causal hypotheses: the LLM proposes, code tests, untestable ones are dropped.

CLAUDE.md has specified this from the start and it was the last piece of
reasoning still hardcoded:

    For causal queries the LLM proposes hypotheses, deterministic code tests
    them, and untested hypotheses are DROPPED, not reported.

Until now ``_build_causal`` tested exactly two explanations -- low productivity
and weak frontal structure -- because those are the two I wrote down. Every
"why has my catch declined" question, in every place, in every season, got the
same two checks.

---------------------------------------------------------------------------
HOW IT WORKS
---------------------------------------------------------------------------

1.  The model is shown what the tools measured and the list of **tests that
    exist**. It proposes explanations, each naming the test that would settle
    it. It writes no numbers; the guard in ``agents/deliberate.py`` strips any.
2.  Each proposal is matched to a :class:`HypothesisTest` -- a real function
    over real tool output. A proposal naming a test we do not have is
    **dropped and never shown**, which is the rule doing its work: an
    untestable explanation is speculation, and speculation printed next to
    measurements reads as a finding.
3.  The test returns supported / not supported plus the statistic it used. The
    model does not get a vote on the outcome.

So the model widens the search and code closes it. A wrong suggestion costs
nothing -- it fails its test, or has none, and disappears.

**The two original hypotheses remain as a floor**, exactly like the rule-based
requests in ``orchestrator/collaborate.py``: if the model is unreachable, rate
limited, or returns nothing usable, the answer is what it was before rather
than empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agents.deliberate import strip_numbers
from orchestrator.llm import client as llm

if TYPE_CHECKING:  # pragma: no cover
    from orchestrator.executor import ExecutionResult

__all__ = ["HypothesisTest", "TESTS", "Proposal", "propose", "TestOutcome"]

PROMPT_PATH = Path(__file__).parent / "prompts" / "causal.md"

#: Standard deviations from the monthly baseline before an anomaly counts as
#: support. Two sigma is the conventional line, and it is a *test threshold*,
#: never a verdict.
ANOMALY_SIGMA = 2.0

#: A front weaker than this is not a front fish would aggregate along. Same
#: figure as tools/ocean/pfz_candidates.py, for the same reason.
MIN_FRONT_GRADIENT = 0.05


@dataclass(frozen=True)
class TestOutcome:
    """What a test concluded, and the number it concluded it from."""

    supported: bool
    statistic: float | None
    description: str
    tool: str


@dataclass(frozen=True)
class HypothesisTest:
    """One thing we can actually check, and the tool that checks it.

    ``blurb`` is what the model is shown. It says what the test decides, never
    what the answer is, so the model chooses tests rather than conclusions.
    """

    id: str
    tool: str
    blurb: str
    run: Callable[[Any], TestOutcome | None]


def _first(result: "ExecutionResult", tool: str):
    for record in result.tool_call_log.values():
        if record.tool == tool and record.step_id:
            output = result.outputs.get(record.step_id)
            if output is not None:
                return output, record.tool_call_id
    return None, None


# --------------------------------------------------------------------------
# The tests. Every one is arithmetic over a tool output.
# --------------------------------------------------------------------------


def _test_low_chlorophyll(output) -> TestOutcome | None:
    sigma = getattr(output, "anomaly_sigma", None)
    if sigma is None:
        return None  # no baseline: not testable, so the hypothesis is dropped
    return TestOutcome(
        supported=sigma <= -ANOMALY_SIGMA,
        statistic=sigma,
        description=(
            f"Chlorophyll anomaly against the per-pixel monthly baseline; "
            f"at or below -{ANOMALY_SIGMA} sigma counts as support."
        ),
        tool="chl_anomaly",
    )


def _test_high_chlorophyll(output) -> TestOutcome | None:
    sigma = getattr(output, "anomaly_sigma", None)
    if sigma is None:
        return None
    return TestOutcome(
        supported=sigma >= ANOMALY_SIGMA,
        statistic=sigma,
        description=(
            f"Chlorophyll anomaly; at or above +{ANOMALY_SIGMA} sigma counts as "
            "support for a bloom or a turbid coastal plume."
        ),
        tool="chl_anomaly",
    )


def _test_no_fronts(output) -> TestOutcome | None:
    gradient = getattr(output, "max_gradient_deg_c_per_km", None)
    if gradient is None:
        return None
    return TestOutcome(
        supported=gradient < MIN_FRONT_GRADIENT,
        statistic=gradient,
        description=(
            f"Strongest sea-surface temperature gradient in the study box; "
            f"below {MIN_FRONT_GRADIENT} C/km means no front worth fishing."
        ),
        tool="thermal_front",
    )


def _test_no_reachable_zone(output) -> TestOutcome | None:
    candidates = getattr(output, "candidates", None)
    if candidates is None:
        return None
    return TestOutcome(
        supported=len(candidates) == 0,
        statistic=float(len(candidates)),
        description=(
            "Count of derived fishing zones within the vessel's range; zero "
            "counts as support."
        ),
        tool="pfz_candidates",
    )


TESTS: dict[str, HypothesisTest] = {
    t.id: t
    for t in (
        HypothesisTest(
            "low_productivity", "chl_anomaly",
            "Plankton is unusually scarce, so there is less food in the water. "
            "Decided by the chlorophyll anomaly against a multi-year baseline "
            "for this month.",
            _test_low_chlorophyll,
        ),
        HypothesisTest(
            "bloom_or_turbidity", "chl_anomaly",
            "Chlorophyll is unusually HIGH, which near this coast can mean a "
            "river plume and turbid water rather than a productive sea. "
            "Decided by the same anomaly, in the other direction.",
            _test_high_chlorophyll,
        ),
        HypothesisTest(
            "weak_frontal_structure", "thermal_front",
            "The temperature boundaries fish gather along have not formed, so "
            "there is nothing concentrating them. Decided by the strongest "
            "temperature gradient measured in the area.",
            _test_no_fronts,
        ),
        HypothesisTest(
            "no_reachable_zone", "pfz_candidates",
            "No fishing zone meets the criteria within this vessel's range, so "
            "the fish may be present but out of reach. Decided by the count of "
            "derived zones.",
            _test_no_reachable_zone,
        ),
    )
}


@dataclass(frozen=True)
class Proposal:
    """One explanation the model offered, matched to a test we can run."""

    test_id: str
    statement: str
    outcome: TestOutcome
    tool_call_id: str | None


def _prompt(result: "ExecutionResult") -> str:
    catalogue = "\n".join(
        f"- {t.id}: {t.blurb}" for t in TESTS.values()
    )
    return PROMPT_PATH.read_text(encoding="utf-8").replace("{TESTS}", catalogue)


def _evidence_summary(result: "ExecutionResult") -> str:
    """What the tools found, without the raw numbers the model must not reuse."""
    lines = []
    for record in result.tool_call_log.values():
        lines.append(f"{record.tool}: status={record.status.value}")
    return json.dumps({"tools_run": sorted(set(lines))}, indent=1)


def propose(result: "ExecutionResult", place: str | None) -> tuple[list[Proposal], str | None]:
    """Ask the model for explanations, then test each one.

    Returns ``(proposals, error)``. A proposal is only returned if a real test
    ran and produced an outcome; anything else is dropped silently, which is
    the behaviour CLAUDE.md requires.
    """
    if not PROMPT_PATH.exists():
        return [], "no causal prompt"

    user = (
        f"Location: {place or 'unspecified'}\n"
        f"What ran this turn:\n{_evidence_summary(result)}\n\n"
        "Propose the explanations worth testing here."
    )
    completion = llm.complete("deliberator", _prompt(result), user)
    if not completion.ok:
        return [], completion.error or "llm unavailable"

    try:
        start, end = completion.text.find("{"), completion.text.rfind("}")
        payload = json.loads(completion.text[start : end + 1])
        raw = payload.get("hypotheses") or []
    except (ValueError, json.JSONDecodeError) as exc:
        return [], f"unparseable: {exc}"

    proposals: list[Proposal] = []
    seen: set[str] = set()

    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        test_id = str(item.get("test_id") or "")
        spec = TESTS.get(test_id)
        # DROPPED: the model proposed something we cannot test. This is the
        # rule, not an error path -- an untestable explanation is speculation.
        if spec is None or test_id in seen:
            continue

        output, call_id = _first(result, spec.tool)
        if output is None:
            continue  # the tool did not run, so the test cannot either
        outcome = spec.run(output)
        if outcome is None:
            continue  # the tool ran but produced nothing the test can use

        seen.add(test_id)
        proposals.append(
            Proposal(
                test_id=test_id,
                statement=strip_numbers(str(item.get("statement") or spec.blurb))[:220],
                outcome=outcome,
                tool_call_id=call_id,
            )
        )

    return proposals, None
