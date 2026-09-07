"""Deterministic dataset catalogue. Lookup, not discovery.

CLAUDE.md: there is no discovery agent. Dataset choice is a deterministic
lookup over `config/datasets.yaml`, which records every source ORCA reads and
the ones it still wants. The planner never browses for data; it selects tools,
and tools read caches filled by ingest/ on a schedule.
"""

from __future__ import annotations

from typing import Any

from core import config

__all__ = ["resolve_datasets", "source_status"]


def resolve_datasets(status: str | None = None) -> list[dict[str, Any]]:
    """Dataset records, optionally filtered by status.

    `status` is one of live | static | in_repo | operator | wanted | rejected.
    A query path asking for anything but live/static/in_repo/operator gets the
    records plus the knowledge they are not usable -- never data.
    """
    records = config.load_yaml("datasets.yaml")
    if status is None:
        return list(records)
    return [r for r in records if r.get("status") == status]


def source_status(source_id: str) -> str | None:
    """Usability of one source id, or None when unlisted."""
    for record in resolve_datasets():
        if record.get("id") == source_id:
            return record.get("status")
    return None
