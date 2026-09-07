"""Progress events for the streaming turn.

A ``ProgressBus`` carries small stage dicts from the pipeline to whoever is
listening (the SSE endpoint). The allowlist lives here: the ONLY stages that
may ever be emitted. No percentages, no prose, no numbers beyond counts the
pipeline already computed -- a progress event must never become a channel
for unverified claims.

The default path uses ``None`` (no bus): ``run_turn()`` behaves bit-for-bit
as today for the CLI and the blocking HTTP route.
"""

from __future__ import annotations

import queue
from typing import Any

#: Every stage the pipeline may report. Anything else is a bug in the caller.
STAGES = (
    "plan",
    "validate",
    "execute",
    "deliberate",
    "collaborate",
    "synthesise",
    "verify",
    "narrate",
    "done",
)


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
