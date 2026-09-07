"""tool_call_id minting and the tool call log.

Every tool invocation gets an id and a record. That record is what the verifier
walks and what the explainability drawer renders, so this module is the hinge
between "a tool ran" and "the answer can be defended".

Ids are per-turn sequential -- ``tc_001``, ``tc_002`` -- rather than random.
Three reasons, in order of how much they matter:

1.  A reasoning trace a human has to read during a demo is far easier to follow
    with ``tc_003`` than with a UUID fragment.
2.  Replaying the same plan produces the same ids, so a recorded turn can be
    re-verified and diffed.
3.  Ordering is visible, which makes a dependency mistake obvious on sight.

They are unique **within a turn**, not globally. The database primary key is
``(turn_id, tool_call_id)``, which is why :class:`ToolCallLog` is created per
turn and never shared between them.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from core.schemas.tool_io import ToolCallRecord, ToolOutput, ToolStatus

__all__ = ["ToolCallLog", "flatten_numbers"]


def flatten_numbers(node: Any) -> list[float]:
    """Every number reachable in a structure, as floats.

    This is the verifier's haystack, so what it excludes matters as much as
    what it includes:

    * **Booleans** are skipped. ``bool`` subclasses ``int`` in Python, and
      letting ``True`` through as ``1.0`` would let a claim of "1" find spurious
      support in any flag a tool happened to return.
    * **Provenance and quality metadata** are excluded by the caller before this
      runs, so a native resolution of 0.05 cannot accidentally satisfy a claim
      about 0.05 of something else. Only a tool's actual results count as
      evidence.
    """
    out: list[float] = []

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            out.append(float(value))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    walk(node)
    return out


class ToolCallLog:
    """The record of everything that ran during one turn.

    Thread-safe because the executor fans steps out across a thread pool, and
    two tools finishing at once must not collide over the id counter.
    """

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._records: dict[str, ToolCallRecord] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def next_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"tc_{self._counter:03d}"

    def record(
        self,
        *,
        tool: str,
        args: dict[str, Any],
        output: ToolOutput | None,
        step_id: str | None = None,
        started_at: datetime | None = None,
        duration_ms: int | None = None,
        status: ToolStatus | None = None,
        error: str | None = None,
        tool_call_id: str | None = None,
    ) -> ToolCallRecord:
        """Write one call into the log and return its record.

        ``output_numbers`` is flattened once, here, rather than on every
        verifier lookup. The verifier checks each number in each claim against
        each cited call, so re-walking nested output every time would be
        wasteful for no benefit.
        """
        call_id = tool_call_id or self.next_id()

        payload: dict[str, Any] = {}
        numbers: list[float] = []
        if output is not None:
            dumped = output.model_dump(mode="json")
            payload = dumped
            # Metadata is deliberately not evidence -- see flatten_numbers.
            results = {
                k: v
                for k, v in dumped.items()
                if k not in ("provenance", "quality", "status", "error")
            }
            numbers = flatten_numbers(results)

        record = ToolCallRecord(
            tool_call_id=call_id,
            tool=tool,
            step_id=step_id,
            turn_id=self.turn_id,
            args=_jsonable(args),
            output=payload,
            output_numbers=numbers,
            status=status or (output.status if output is not None else ToolStatus.FAILED),
            error=error or (output.error if output is not None else None),
            started_at=started_at or datetime.now(timezone.utc),
            duration_ms=duration_ms,
        )
        with self._lock:
            self._records[call_id] = record
        return record

    def as_dict(self) -> dict[str, ToolCallRecord]:
        """The mapping the verifier takes. A copy, so it cannot be mutated."""
        with self._lock:
            return dict(self._records)

    def failed(self) -> list[ToolCallRecord]:
        return [r for r in self._records.values() if r.status is ToolStatus.FAILED]

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, call_id: object) -> bool:
        return call_id in self._records


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of tool arguments for storage.

    Arguments can hold Pydantic models -- a Range handed from one step to the
    next -- and those must not reach the database as reprs.
    """
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value
