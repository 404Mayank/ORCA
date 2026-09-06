"""Health and readiness.

``/health`` answers "is the process up". ``/readiness`` answers the question
that actually matters before a demo: **is there data in the cache, and how old
is it?**

They are separate on purpose. The process is up long before it can answer
anything useful, and a green health check on an empty cache is how you walk
into a demo and discover every tool returns FAILED.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from core import config
from ingest.sources import erddap, gdacs, open_meteo
from orchestrator.llm.client import provider_status
from orchestrator.session import SESSIONS
from tools import registry

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness. Cheap, no I/O beyond the registry."""
    specs = registry.all_specs()
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "tools": {
            "registered": len(specs),
            "implemented": sum(1 for s in specs if s.implemented),
        },
        "sessions": len(SESSIONS),
    }


@router.get("/readiness")
def readiness() -> dict[str, Any]:
    """Whether the cache can actually answer a question, and how stale it is.

    ``ready`` is False when the weather cache is missing, because that is the
    layer every safety answer needs. The ocean and alert layers degrade the
    answer rather than preventing it, so they are reported but not gating.
    """
    layers: dict[str, Any] = {}

    lat, lon = 10.77, 79.84  # the demo origin
    marine = open_meteo.load_cached("marine", lat, lon)
    layers["weather"] = {
        "cached": marine is not None,
        "retrieved_at": marine.fetched_at.isoformat() if marine else None,
        "age_hours": (
            round((datetime.now(timezone.utc) - marine.fetched_at).total_seconds() / 3600, 2)
            if marine
            else None
        ),
    }

    for label, spec in (("sst", erddap.MUR_SST), ("chlorophyll", erddap.CHL_GAPFILLED)):
        grid = erddap.load_cached(spec["dataset_id"])
        layers[label] = {
            "cached": grid is not None,
            "dataset": spec["dataset_id"],
            "observation_age_days": round(grid.age_days, 2) if grid else None,
        }

    cached_alerts = gdacs.load_cached()
    layers["alerts"] = {
        # None here means UNCHECKED, never "nothing in force". A clean verdict
        # is unavailable while this is False, whatever the sea is doing.
        "cached": cached_alerts is not None,
        "in_force": len(cached_alerts[0]) if cached_alerts else None,
        "retrieved_at": cached_alerts[1].isoformat() if cached_alerts else None,
    }

    ready = bool(layers["weather"]["cached"])
    return {
        "ready": ready,
        "hint": None if ready else "run: python scripts/refresh_cache.py",
        "layers": layers,
        "llm_providers": provider_status(),
        "bbox": config.bbox(),
    }
