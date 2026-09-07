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


def _freshness_limits() -> tuple[float, float]:
    """(forecast_max_age_hours, satellite_max_age_days) from risk_thresholds.yaml.

    Falls back to the documented defaults if the file ever loses the block --
    readiness must degrade, not 500, on a config gap.
    """
    try:
        fresh = config.risk_thresholds().get("data_freshness", {})
        forecast_h = float(fresh.get("forecast_max_age_h", {}).get("value", 12.0))
        satellite_d = float(fresh.get("satellite_max_age_days", {}).get("value", 7.0))
    except (ValueError, TypeError, AttributeError, KeyError):
        forecast_h, satellite_d = 12.0, 7.0
    return forecast_h, satellite_d


def _operator_table_status(now: datetime) -> dict[str, Any]:
    """Freshness of config/active_alerts.yaml, by the same rule active_alerts uses.

    The authority for the rule is ``tools/weather/alerts.py::_load_operator_table``
    (``review_max_age_hours``; over-limit means unchecked, never empty). This
    mirrors it for display so the Settings "Alerts" row cannot claim "cached"
    while the wave half is stale: the alerts layer stays cyclone-scoped
    (GDACS), and these facts let the UI say "cyclone check only, wave table
    stale" instead of a bare "cached".
    """
    try:
        raw = config.load_yaml("active_alerts.yaml")
    except FileNotFoundError:
        return {"checked": False, "reviewed_at": None, "age_hours": None, "max_age_hours": 24.0}
    meta = raw.get("meta") or {}
    try:
        max_age = float(meta.get("review_max_age_hours", 24))
    except (ValueError, TypeError):
        max_age = 24.0
    reviewed_raw = meta.get("last_reviewed")
    reviewed: datetime | None = None
    if reviewed_raw:
        try:
            reviewed = datetime.fromisoformat(str(reviewed_raw))
        except ValueError:
            reviewed = None
    if reviewed is None:
        return {
            "checked": False,
            "reviewed_at": None,
            "age_hours": None,
            "max_age_hours": max_age,
        }
    age_hours = round((now - reviewed).total_seconds() / 3600, 2)
    return {
        "checked": age_hours <= max_age,
        "reviewed_at": reviewed.isoformat(),
        "age_hours": age_hours,
        "max_age_hours": max_age,
    }


@router.get("/readiness")
def readiness() -> dict[str, Any]:
    """Whether the cache can actually answer a question, and how stale it is.

    ``ready`` is False when the weather cache is missing **or stale** (older
    than ``data_freshness.forecast_max_age_h``), because that is the layer
    every safety answer needs. The ocean and alert layers degrade the answer
    rather than preventing it, so they are reported but not gating.

    ``stale`` vs ``cached``: a stale layer still has a file on disk, but the
    file is too old to support a verdict, so ``cached`` is False and ``stale``
    is True. The UI renders that as "stale", never as a healthy "cached".

    The ``alerts`` layer covers **cyclones only** (GDACS, 12 h rule enforced
    inside ``gdacs.load_cached``). High-wave / swell-surge coverage comes from
    the operator table, whose freshness is reported alongside in
    ``operator_*`` so the row cannot pass as a full check when the table is
    overdue. It is deliberately not folded into ``cached``: a stale wave table
    must not erase the genuine cyclone negative finding, and a fresh cyclone
    cache must not mask the stale wave table. Both facts are shown.
    """
    now = datetime.now(timezone.utc)
    forecast_max_h, satellite_max_d = _freshness_limits()
    layers: dict[str, Any] = {}

    lat, lon = 10.77, 79.84  # the demo origin
    marine = open_meteo.load_cached("marine", lat, lon)
    if marine is None:
        layers["weather"] = {
            "cached": False,
            "retrieved_at": None,
            "age_hours": None,
            "stale": False,
            "max_age_hours": forecast_max_h,
        }
    else:
        age_hours = round((now - marine.fetched_at).total_seconds() / 3600, 2)
        stale = age_hours > forecast_max_h
        layers["weather"] = {
            "cached": not stale,
            "retrieved_at": marine.fetched_at.isoformat(),
            "age_hours": age_hours,
            "stale": stale,
            "max_age_hours": forecast_max_h,
        }

    for label, spec in (("sst", erddap.MUR_SST), ("chlorophyll", erddap.CHL_GAPFILLED)):
        grid = erddap.load_cached(spec["dataset_id"])
        if grid is None:
            layers[label] = {
                "cached": False,
                "dataset": spec["dataset_id"],
                "observation_age_days": None,
                "retrieved_at": None,
                "stale": False,
                "max_age_days": satellite_max_d,
            }
        else:
            observation_age = round(grid.age_days, 2)
            stale = grid.age_days > satellite_max_d
            layers[label] = {
                "cached": not stale,
                "dataset": spec["dataset_id"],
                "observation_age_days": observation_age,
                "retrieved_at": grid.fetched_at.isoformat(),
                "stale": stale,
                "max_age_days": satellite_max_d,
            }

    cached_alerts = gdacs.load_cached()
    operator = _operator_table_status(now)
    if cached_alerts is None:
        alerts_age: float | None = None
    else:
        alerts_age = round((now - cached_alerts[1]).total_seconds() / 3600, 2)
    layers["alerts"] = {
        # None here means UNCHECKED, never "nothing in force". A clean verdict
        # is unavailable while this is False, whatever the sea is doing.
        # Cyclone-scoped: GDACS only. See the docstring.
        "cached": cached_alerts is not None,
        "in_force": len(cached_alerts[0]) if cached_alerts else None,
        "retrieved_at": cached_alerts[1].isoformat() if cached_alerts else None,
        "age_hours": alerts_age,
        "operator_checked": operator["checked"],
        "operator_age_hours": operator["age_hours"],
        "operator_max_age_hours": operator["max_age_hours"],
        "operator_reviewed_at": operator["reviewed_at"],
    }

    ready = bool(layers["weather"]["cached"])
    if ready:
        hint = None
    elif layers["weather"]["stale"]:
        hint = (
            f"weather cache is stale (> {forecast_max_h:.0f} h old); "
            "run: python scripts/refresh_cache.py --weather"
        )
    else:
        hint = "run: python scripts/refresh_cache.py"
    return {
        "ready": ready,
        "hint": hint,
        "layers": layers,
        "llm_providers": provider_status(),
        "bbox": config.bbox(),
    }
