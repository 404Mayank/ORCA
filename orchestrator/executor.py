"""Runs a validated plan: fans steps out in parallel, resolves references,
records every call.

By the time a plan reaches here it has already passed ``validate_plan()``, so
the executor does not re-litigate whether a tool exists or whether arguments
type-check. Its job is narrower: run the DAG, wire outputs into inputs, keep
time, and write the log the verifier will read.

**Failure policy**, which is the part with real consequences:

* A failing step does **not** abort the run. The other branches still produce
  what they can, and the answer degrades rather than disappearing. A fisherman
  who gets wave heights and a caveat about the missing alert check is better
  served than one who gets a 500.
* A step is skipped only when a dependency produced **no output object at all**
  -- it raised, or timed out. Running ``compute_risk_score`` on a null wave
  height would produce a confident number from nothing, which is precisely the
  failure the governing rule exists to prevent.
* A dependency that returned a **structured failure** does not block its
  dependents. This distinction is load-bearing. ``active_alerts`` returns a
  well-formed ``ActiveAlertsOut`` with ``status=FAILED`` and ``checked=False``;
  ``compute_risk_score`` is built to consume exactly that and downgrade the
  verdict to ``no_go``. Skipping the risk step instead would produce *no
  verdict*, which is a worse answer than a cautious one -- the fisherman
  learns nothing rather than learning to stay in.
* ``optional`` steps degrade quietly; non-optional ones set ``degraded`` on the
  result so the synthesis layer must account for them. ``validate_plan()``
  already guarantees no safety-critical step is marked optional.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

import tools  # noqa: F401 -- populates the registry
from core.provenance import ToolCallLog
from core.schemas.plan import Plan, parse_reference
from core.schemas.recommendation import ReasoningStep
from core.schemas.tool_io import ToolCallRecord, ToolOutput, ToolStatus
from tools import registry

__all__ = ["ExecutionResult", "execute_plan", "execute_plan_sync"]

#: Per-step ceiling when the plan does not set one. Generous enough for a cold
#: cache read, tight enough that one wedged step cannot eat a demo.
DEFAULT_TIMEOUT_S = 20.0


@dataclass
class ExecutionResult:
    """Everything one plan run produced."""

    log: ToolCallLog
    outputs: dict[str, ToolOutput] = field(default_factory=dict)
    call_ids: dict[str, str] = field(default_factory=dict)
    trace: list[ReasoningStep] = field(default_factory=list)
    degraded: bool = False
    degradation_notes: list[str] = field(default_factory=list)

    @property
    def tool_call_log(self) -> dict[str, ToolCallRecord]:
        """The mapping ``orchestrator.verifier.verify()`` takes."""
        return self.log.as_dict()

    def output_for(self, step_id: str) -> ToolOutput | None:
        return self.outputs.get(step_id)

    def call_id_for(self, step_id: str) -> str | None:
        """The ``tool_call_id`` a step produced, for citing it as evidence."""
        return self.call_ids.get(step_id)

    def succeeded(self, step_id: str) -> bool:
        out = self.outputs.get(step_id)
        return out is not None and out.status is not ToolStatus.FAILED


def _resolve_value(value: Any, outputs: dict[str, ToolOutput]) -> Any:
    """Substitute ``$sN`` and ``$sN.field`` references with real values.

    Walks nested structures, because a reference can legitimately sit inside a
    list of points or a nested options object.
    """
    if isinstance(value, str):
        reference = parse_reference(value)
        if reference is None:
            return value
        step_id, path = reference
        current: Any = outputs.get(step_id)
        for attribute in path:
            if current is None:
                return None
            current = (
                getattr(current, attribute, None)
                if isinstance(current, BaseModel)
                else (current or {}).get(attribute)
                if isinstance(current, dict)
                else None
            )
        return current
    if isinstance(value, dict):
        return {k: _resolve_value(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, outputs) for v in value]
    return value


async def _run_step(step, outputs: dict[str, ToolOutput], log: ToolCallLog):
    """Execute one step. Never raises -- failures come back as records."""
    spec = registry.get(step.tool)
    started = time.perf_counter()
    resolved = _resolve_value(step.args, outputs)

    try:
        model = spec.input_model.model_validate(resolved)
    except ValidationError as exc:
        # Arguments were valid at plan time; if they are invalid now, a
        # reference resolved to something unexpected. Worth a distinct message,
        # because it points at the data flow rather than at the planner.
        record = log.record(
            tool=step.tool,
            args=resolved,
            output=None,
            step_id=step.id,
            status=ToolStatus.FAILED,
            error=f"resolved arguments failed validation: {exc.errors()[0]['msg']}",
            duration_ms=0,
        )
        return step, None, record

    timeout = step.timeout_s or DEFAULT_TIMEOUT_S
    try:
        # Tools are ordinary synchronous functions -- they do file and CPU work,
        # never network, since a query must not touch the network. A thread
        # keeps one slow tool from blocking the whole layer.
        #
        # LIMITATION, stated because it is not obvious: a timeout unblocks the
        # PLAN, not the thread. Python cannot cancel a running thread, so a
        # wedged tool keeps its worker until it returns on its own, and the
        # event loop waits for the pool at shutdown. The plan proceeds
        # immediately and the step is recorded as failed at the timeout, which
        # is what matters for the answer; but a genuinely hung tool can still
        # delay process exit. Acceptable because every tool here reads a local
        # cache or does bounded CPU work. It would NOT be acceptable if a tool
        # ever did network I/O -- another reason queries never fetch.
        output = await asyncio.wait_for(asyncio.to_thread(spec.fn, model), timeout)
    except asyncio.TimeoutError:
        record = log.record(
            tool=step.tool,
            args=resolved,
            output=None,
            step_id=step.id,
            status=ToolStatus.FAILED,
            error=f"timed out after {timeout:.0f}s",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return step, None, record
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        record = log.record(
            tool=step.tool,
            args=resolved,
            output=None,
            step_id=step.id,
            status=ToolStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return step, None, record

    record = log.record(
        tool=step.tool,
        args=resolved,
        output=output,
        step_id=step.id,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return step, output, record


async def execute_plan(
    plan: Plan, turn_id: str = "t_001", into: ExecutionResult | None = None
) -> ExecutionResult:
    """Run a validated plan and return everything it produced.

    Steps run in the parallel waves ``Plan.execution_layers()`` computes, which
    is what lets the four agent groups work concurrently where the DAG allows.

    ``into`` continues an existing run rather than starting one. This is what
    ``orchestrator/collaborate.py`` uses to execute a follow-up round: the same
    log, the same tool_call_id sequence, and earlier steps' outputs still
    resolvable, so a step an agent requested can reference ``$s1.lat`` from the
    original plan. A fresh ExecutionResult per round would give the verifier
    two disjoint logs for one answer, and a claim citing a first-round call
    would fail to verify.
    """
    result = into if into is not None else ExecutionResult(log=ToolCallLog(turn_id))
    log = result.log
    by_id = plan.step_by_id()
    skipped: set[str] = set()

    for layer in plan.execution_layers():
        runnable = []
        for step_id in layer:
            step = by_id[step_id]
            # Only a dependency that produced NO output blocks a dependent.
            # A structured failure is still data, and a tool built to consume
            # it -- compute_risk_score reading alerts_checked=False -- must
            # still run, or a failed alert check yields no verdict instead of
            # a cautious one.
            broken = [
                d
                for d in step.depends_on
                if d in by_id and (d in skipped or d not in result.outputs)
            ]
            if broken:
                # Do not run a step on inputs that do not exist. A risk score
                # computed from a null wave height is a confident number from
                # nothing.
                skipped.add(step_id)
                result.trace.append(
                    ReasoningStep(
                        step=step_id,
                        tool=step.tool,
                        status="skipped",
                        note=f"dependency failed: {broken}",
                    )
                )
                result.degraded = True
                result.degradation_notes.append(
                    f"{step.tool} was skipped because {broken} did not produce a result."
                )
                continue
            runnable.append(step)

        if not runnable:
            continue

        for step, output, record in await asyncio.gather(
            *(_run_step(s, result.outputs, log) for s in runnable)
        ):
            result.call_ids[step.id] = record.tool_call_id
            if output is not None:
                result.outputs[step.id] = output

            status = record.status.value
            result.trace.append(
                ReasoningStep(
                    step=step.id,
                    tool=step.tool,
                    status="ok" if status == "ok" else status,
                    ms=record.duration_ms,
                    note=record.error,
                    tool_call_id=record.tool_call_id,
                )
            )

            if record.status is ToolStatus.FAILED:
                result.degraded = True
                note = f"{step.tool} failed: {record.error}"
                if step.optional:
                    note = f"{note} (optional step)"
                result.degradation_notes.append(note)
            elif record.status is ToolStatus.DEGRADED:
                result.degraded = True
                result.degradation_notes.append(
                    f"{step.tool} returned degraded data: {record.error}"
                )

    return result


def execute_plan_sync(
    plan: Plan, turn_id: str = "t_001", into: ExecutionResult | None = None
) -> ExecutionResult:
    """Blocking wrapper, for scripts, tests and the CLI demo."""
    return asyncio.run(execute_plan(plan, turn_id, into))
