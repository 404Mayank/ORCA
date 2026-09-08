"""Progress events for the streaming turn.

A ``ProgressBus`` carries small stage dicts from the pipeline to whoever is
listening (the SSE endpoint). The allowlist lives here: the ONLY stages that
may ever be emitted. No percentages and no numbers beyond counts the
pipeline already computed -- a progress event must never become a channel
for unverified figures. Short model-written status lines may ride along
(deliberation assessments); they are number-stripped at their source and
capped, and must never carry a measurement.

The default path uses ``None`` (no bus): ``run_turn()`` behaves bit-for-bit
as today for the CLI and the blocking HTTP route.
"""

from __future__ import annotations

import queue
from typing import Any

#: Every stage the pipeline may report. Anything else is a bug in the caller.
#: There is deliberately no "validate": validation observably happens inside
#: plan_query (a plan exists iff it passed), so a validate event emitted from
#: outside would be invented. Its outcome rides on the plan event instead.
STAGES = (
    "plan",
    "execute",
    "deliberate",
    "collaborate",
    "synthesise",
    "verify",
    "narrate",
    "done",
)


#: Optional ``phase`` on an event. Absent means the stage completed, which is
#: what every event meant before this field existed and still means for the
#: stages that emit once.
#:
#: ``"start"`` is for the two stages that wrap a model call long enough to
#: look like a hang: the planner, and the parallel deliberation round. It says
#: only that the work began, and it carries no conclusions -- the planner's
#: start frame has no ``query_type``, the deliberation's has no assessments.
#: That distinction is the whole reason a phase field is safe: reporting that
#: something is running is an observation, reporting what it found before it
#: has finished would be invention.
PHASES = ("start", "end")


class ProgressBus:
    """Thread-safe event queue. One per streaming turn, never shared."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._seq = 0

    def emit(self, stage: str, **detail: Any) -> None:
        """Push one event. Unknown stages raise: inventing progress is the
        failure mode this module exists to prevent."""
        if stage not in STAGES:
            raise ValueError(f"unknown progress stage {stage!r}")
        self._seq += 1
        self._queue.put({"seq": self._seq, "stage": stage, **detail})

    def close(self) -> None:
        """No more events will come. The consumer stops on this sentinel."""
        self._queue.put(None)

    def drain(self, timeout: float = 0.5) -> list[dict[str, Any] | None]:
        """Whatever has arrived, without blocking past timeout."""
        events: list[dict[str, Any] | None] = []
        try:
            events.append(self._queue.get(timeout=timeout))
        except queue.Empty:
            return events
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                return events
