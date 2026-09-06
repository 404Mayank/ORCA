"""Tests for the executor and the tool call log.

The executor's job is narrow -- run a validated DAG, wire outputs into inputs,
record everything -- but its *failure policy* has real consequences, and that
is what most of these tests are about.
"""

from __future__ import annotations

import time

import pytest
from pydantic import Field

from core.provenance import ToolCallLog, flatten_numbers
from core.schemas.intent import QueryType
from core.schemas.plan import Plan
from core.schemas.tool_io import Provenance, ToolInput, ToolOutput, ToolStatus
from orchestrator.executor import execute_plan_sync
from orchestrator.validate_plan import fallback_plan, validate_plan
from tools import registry


# --------------------------------------------------------------------------
# Fake tools, registered once at import under names no real tool uses.
# --------------------------------------------------------------------------


class _EchoIn(ToolInput):
    value: float = 1.0


class _EchoOut(ToolOutput):
    value: float = 0.0
    flag: bool = True


def _echo(args: _EchoIn) -> _EchoOut:
    return _EchoOut(provenance=Provenance(source="test"), value=args.value)


def _explode(args: _EchoIn) -> _EchoOut:
    raise RuntimeError("boom")


def _structured_failure(args: _EchoIn) -> _EchoOut:
    """Returns a well-formed object that reports its own failure.

    This is the shape active_alerts uses, and the reason the distinction
    between 'no output' and 'a failure object' matters.
    """
    return _EchoOut(
        provenance=Provenance(source="test"),
        status=ToolStatus.FAILED,
        error="could not check",
        value=0.0,
    )


def _slow(args: _EchoIn) -> _EchoOut:
    time.sleep(2.0)
    return _EchoOut(provenance=Provenance(source="test"), value=args.value)


for _name, _fn in [
    ("t_echo", _echo),
    ("t_explode", _explode),
    ("t_structured_failure", _structured_failure),
    ("t_slow", _slow),
]:
    if not registry.has(_name):
        registry.register(
            _name,
            description=f"test tool {_name}",
            input_model=_EchoIn,
            output_model=_EchoOut,
            agent="risk",
            fn=_fn,
        )


def _plan(steps, intent=QueryType.GEOFENCE_CHECK) -> Plan:
    return Plan.model_validate({"intent_type": intent.value, "steps": steps})


# ==========================================================================
# Failure policy -- the part with consequences
# ==========================================================================


def test_a_structured_failure_does_not_block_its_dependents():
    """The rule that keeps a failed alert check from erasing the verdict.

    active_alerts returns a well-formed object with status=FAILED, and
    compute_risk_score is built to read that and downgrade. If the executor
    skipped dependents of any failed step, the fisherman would get no verdict
    at all -- strictly worse than a cautious one.
    """
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_structured_failure", "args": {"value": 1.0}},
                {
                    "id": "s2",
                    "tool": "t_echo",
                    "args": {"value": "$s1.value"},
                    "depends_on": ["s1"],
                },
            ]
        )
    )
    assert result.output_for("s2") is not None, "dependent must still have run"
    assert result.degraded
    assert [t.status for t in result.trace] == ["failed", "ok"]


def test_a_tool_that_raises_does_block_its_dependents():
    """No output at all means the dependent would run on nothing.

    A risk score computed from a null wave height is a confident number
    produced from nowhere, which is exactly what the governing rule forbids.
    """
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_explode", "args": {"value": 1.0}},
                {
                    "id": "s2",
                    "tool": "t_echo",
                    "args": {"value": "$s1.value"},
                    "depends_on": ["s1"],
                },
            ]
        )
    )
    assert result.output_for("s2") is None
    statuses = {t.step: t.status for t in result.trace}
    assert statuses["s1"] == "failed"
    assert statuses["s2"] == "skipped"
    assert any("dependency failed" in (t.note or "") for t in result.trace)


def test_a_raised_exception_is_recorded_not_swallowed():
    result = execute_plan_sync(
        _plan([{"id": "s1", "tool": "t_explode", "args": {"value": 1.0}}])
    )
    record = result.tool_call_log[result.call_id_for("s1")]
    assert record.status is ToolStatus.FAILED
    assert "RuntimeError" in record.error and "boom" in record.error


def test_a_timeout_gives_up_on_the_step_quickly():
    """One wedged tool must not eat the demo.

    Asserts what the timeout actually guarantees: the STEP is abandoned at its
    deadline and recorded as failed. It deliberately does not assert that the
    whole run finishes quickly, because Python cannot cancel a running thread
    -- the worker keeps going until it returns and the loop waits for the pool
    at shutdown. The plan is unblocked immediately, which is what determines
    the answer. See the note in orchestrator/executor.py.
    """
    result = execute_plan_sync(
        _plan([{"id": "s1", "tool": "t_slow", "args": {"value": 1.0}, "timeout_s": 0.2}])
    )
    record = result.tool_call_log[result.call_id_for("s1")]
    assert record.status is ToolStatus.FAILED
    assert "timed out" in record.error
    assert record.duration_ms < 1000, "the step should give up at its deadline"


def test_a_timeout_does_not_stop_a_parallel_branch_from_finishing():
    """The property that actually protects the answer."""
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_slow", "args": {"value": 1.0}, "timeout_s": 0.2},
                {"id": "s2", "tool": "t_echo", "args": {"value": 9.0}},
            ]
        )
    )
    assert result.output_for("s2").value == 9.0
    assert result.degraded


def test_one_branch_failing_does_not_abort_the_others():
    """A partial answer beats a 500."""
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_explode", "args": {"value": 1.0}},
                {"id": "s2", "tool": "t_echo", "args": {"value": 7.0}},
            ]
        )
    )
    assert result.output_for("s2").value == 7.0
    assert result.degraded


# ==========================================================================
# Reference resolution
# ==========================================================================


def test_references_are_resolved_between_steps():
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_echo", "args": {"value": 42.0}},
                {
                    "id": "s2",
                    "tool": "t_echo",
                    "args": {"value": "$s1.value"},
                    "depends_on": ["s1"],
                },
            ]
        )
    )
    assert result.output_for("s2").value == 42.0


def test_steps_run_in_parallel_layers():
    """The four agent groups must actually fan out, not run in sequence."""
    plan = _plan(
        [
            {"id": "s1", "tool": "t_echo", "args": {"value": 1.0}},
            {"id": "s2", "tool": "t_echo", "args": {"value": "$s1.value"}, "depends_on": ["s1"]},
            {"id": "s3", "tool": "t_echo", "args": {"value": "$s1.value"}, "depends_on": ["s1"]},
        ]
    )
    assert plan.execution_layers() == [["s1"], ["s2", "s3"]]
    result = execute_plan_sync(plan)
    assert len(result.log) == 3


# ==========================================================================
# The log
# ==========================================================================


def test_tool_call_ids_are_sequential_and_readable():
    """tc_003 is far easier to follow in a live demo than a UUID fragment."""
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_echo", "args": {"value": 1.0}},
                {"id": "s2", "tool": "t_echo", "args": {"value": 2.0}},
            ]
        )
    )
    assert sorted(result.tool_call_log) == ["tc_001", "tc_002"]


def test_the_log_records_arguments_as_actually_resolved():
    """Not the plan's '$s1.value' placeholder -- the value it became.

    The drawer has to show what the tool was really called with, or the trace
    is a description of intent rather than of what happened.
    """
    result = execute_plan_sync(
        _plan(
            [
                {"id": "s1", "tool": "t_echo", "args": {"value": 5.0}},
                {
                    "id": "s2",
                    "tool": "t_echo",
                    "args": {"value": "$s1.value"},
                    "depends_on": ["s1"],
                },
            ]
        )
    )
    record = result.tool_call_log[result.call_id_for("s2")]
    assert record.args == {"value": 5.0}


def test_output_numbers_exclude_booleans():
    """bool subclasses int; letting True through as 1.0 would give a claim of
    '1' spurious support from any flag a tool happened to return."""
    result = execute_plan_sync(
        _plan([{"id": "s1", "tool": "t_echo", "args": {"value": 3.0}}])
    )
    record = result.tool_call_log[result.call_id_for("s1")]
    assert record.output_numbers == [3.0]


def test_flatten_numbers_walks_nested_structures():
    assert sorted(flatten_numbers({"a": 1, "b": [2, {"c": 3}], "d": True, "e": "x"})) == [
        1.0,
        2.0,
        3.0,
    ]


def test_log_ids_are_unique_under_concurrency():
    log = ToolCallLog("t_x")
    import threading

    ids: list[str] = []
    lock = threading.Lock()

    def mint():
        got = log.next_id()
        with lock:
            ids.append(got)

    threads = [threading.Thread(target=mint) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 50


# ==========================================================================
# The real plans, end to end
# ==========================================================================


def test_geofence_query_runs_end_to_end():
    plan = validate_plan(fallback_plan(QueryType.GEOFENCE_CHECK, {"PLACE": "Rameswaram"})).plan
    result = execute_plan_sync(plan, turn_id="t_geo")
    assert result.succeeded("s1")
    fence = result.output_for("s2")
    assert fence is not None
    assert fence.zones_checked == ["imbl"]


def test_safety_query_produces_a_verdict_despite_the_failed_alert_check(no_alert_sources):
    """The whole point of the failure policy, on the real plan.

    Every weather tool succeeds, active_alerts fails because the fixture
    makes every source unreachable, and compute_risk_score still runs --
    returning no_go with the alert check named as the reason rather than the
    sea state.

    The executor once skipped the risk step on a failed dependency here, and
    the result was *no verdict at all*. A cautious verdict beats silence.
    """
    plan = validate_plan(
        fallback_plan(
            QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
        )
    ).plan
    result = execute_plan_sync(plan, turn_id="t_safety")

    risk = result.output_for("s6")
    if result.output_for("s2") is None:
        pytest.skip("no Open-Meteo cache present; run ingest first")

    assert risk is not None, "a failed alert check must not erase the verdict"
    assert risk.verdict == "no_go"
    assert risk.downgraded
    assert "alert check" in risk.downgrade_reason
    assert result.degraded


def test_the_safety_plan_reads_alerts_checked_rather_than_asserting_it():
    """A plan that hardcoded alerts_checked=True would tell the risk function
    the cyclone check succeeded even when it did not."""
    raw = fallback_plan(
        QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"}
    )
    risk_step = next(s for s in raw["steps"] if s["tool"] == "compute_risk_score")
    assert risk_step["args"]["alerts_checked"] == "$s5.checked"
    assert risk_step["args"]["alerts_active"] == "$s5.count"
