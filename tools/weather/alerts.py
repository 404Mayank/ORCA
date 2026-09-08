"""Active advisory lookup: IMD cyclone bulletins, INCOIS high-wave and swell surge.

SAFETY-CRITICAL. An empty result is a real result -- it is what makes
"no cyclone is active" a citable negative finding rather than an assumption.
A FAILED call is not an empty result, and the two must never be conflated:
not knowing whether a cyclone is active is not the same as there being none.
core.schemas.recommendation refuses to build a `go` verdict alongside a failed
call, which is where that rule is enforced.

Normalised shape from CLAUDE.md: {type, severity, zone, issued_at, valid_until,
authority, text}.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from core.schemas.tool_io import ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class Advisory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="cyclone | high_wave | swell_surge | lightning")
    severity: str
    zone: str
    issued_at: datetime | None = None
    valid_until: datetime | None = None
    authority: str = Field(description="IMD or INCOIS.")
    text: str = ""


class ActiveAlertsIn(ToolInput):
    district: str | None = None
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    types: list[str] = Field(default_factory=lambda: ["cyclone", "high_wave", "swell_surge"])


class ActiveAlertsOut(ToolOutput):
    count: int = Field(ge=0)
    alerts: list[Advisory] = Field(default_factory=list)
    checked_types: list[str] = Field(default_factory=list, description="What was actually checked, so a partial check cannot pass as a full one.")
    checked: bool = Field(
        default=False,
        description=(
            "True only when the check actually completed. This is the single "
            "bit compute_risk_score reads to decide whether it may issue a "
            "clean verdict. It defaults to False so that a tool which forgets "
            "to set it fails safe."
        ),
    )


register(
    "active_alerts",
    description="Advisories in force for a district or point. An empty list is a citable negative finding.",
    input_model=ActiveAlertsIn,
    output_model=ActiveAlertsOut,
    agent=AgentGroup.WEATHER,
    safety_critical=True,
    notes="A failed call must downgrade the verdict; it may never be read as 'no alerts'.",
)


# ==========================================================================
# Implementation
# ==========================================================================
#
# Wired 2026-09-06. Until then this function returned FAILED unconditionally,
# which forced every safety verdict to no_go. Two sources now stand behind it:
#
#   cyclone                  GDACS, live and keyless (ingest/sources/gdacs.py)
#   high_wave, swell_surge   operator table (config/active_alerts.yaml)
#   lightning                nothing. Never reported as checked.
#
# The rule that governs all of it is unchanged and is now enforced per type
# rather than globally: a type is `checked` only when a source that covers it
# was actually reachable. Asking for cyclone on a day GDACS answers is a
# complete check. Asking for lightning never is.

from datetime import datetime, timezone

import yaml

from alerts.rules import (
    ALERT_TYPES,
    SEVERITY_ORDER,
    checked_types as _checked_types,
    coverage_gaps,
    within_validity,
)
from core import config
from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from ingest.sources import gdacs
from tools import registry

_OPERATOR_TABLE = "active_alerts.yaml"


def _load_operator_table() -> tuple[list[Advisory], datetime | None, list[str]]:
    """Read the operator table. Returns (advisories, last_reviewed, notes).

    A table reviewed too long ago is treated as **not checked**, not as empty.
    A file nobody has opened in a week is not evidence that the sea is calm.
    """
    notes: list[str] = []
    try:
        raw = config.load_yaml(_OPERATOR_TABLE)
    except FileNotFoundError:
        return [], None, [f"{_OPERATOR_TABLE} is missing"]

    meta = raw.get("meta") or {}
    reviewed_raw = meta.get("last_reviewed")
    reviewed: datetime | None = None
    if reviewed_raw:
        try:
            reviewed = datetime.fromisoformat(str(reviewed_raw).replace("Z", "+00:00"))
        except ValueError:
            notes.append(f"{_OPERATOR_TABLE}: unparseable last_reviewed {reviewed_raw!r}")

    if reviewed is None:
        return [], None, notes + [f"{_OPERATOR_TABLE} has never been reviewed"]

    max_age = float(meta.get("review_max_age_hours", 24))
    age_hours = (datetime.now(timezone.utc) - reviewed).total_seconds() / 3600.0
    if age_hours > max_age:
        return [], None, notes + [
            f"{_OPERATOR_TABLE} last reviewed {age_hours:.0f} h ago, over the "
            f"{max_age:.0f} h limit; treated as unchecked rather than as empty"
        ]

    advisories: list[Advisory] = []
    for entry in raw.get("advisories") or []:
        try:
            advisory = Advisory(
                type=str(entry["type"]),
                severity=str(entry["severity"]),
                zone=str(entry.get("zone", "")),
                issued_at=entry.get("issued_at"),
                valid_until=entry.get("valid_until"),
                authority=str(entry.get("authority", "INCOIS")),
                text=str(entry.get("text", "")).strip(),
            )
        except (KeyError, ValueError) as exc:
            notes.append(f"{_OPERATOR_TABLE}: skipped malformed entry ({exc})")
            continue
        if advisory.type not in ALERT_TYPES:
            notes.append(f"{_OPERATOR_TABLE}: unknown type {advisory.type!r}, skipped")
            continue
        if not within_validity(advisory.issued_at, advisory.valid_until):
            notes.append(f"{_OPERATOR_TABLE}: {advisory.type} advisory expired, skipped")
            continue
        advisories.append(advisory)

    return advisories, reviewed, notes


def _derive_wave(
    lat: float, lon: float, district: str | None
) -> tuple[list[dict] | None, list[str]]:
    """Derived wave advisories at a point, or ``None`` if nothing was read.

    ``None`` and ``[]`` mean different things here, exactly as they do for
    the tool as a whole: ``None`` is "no cached forecast, so this source
    could not be interrogated" and must not add itself to ``live_sources``,
    while ``[]`` is "we looked and the sea is under every published band",
    which is the result that licenses a negative finding.
    """
    from alerts.derived import derive_wave_advisories
    from tools.weather.wave_forecast import WaveForecastIn, wave_forecast

    try:
        forecast = wave_forecast(
            WaveForecastIn(lat=lat, lon=lon, hours=24)
        )
    except Exception as exc:  # noqa: BLE001 -- a wave read must not sink the alert check
        return None, [f"wave cache unreadable, no derived advisory: {exc}"]

    if forecast.status is ToolStatus.FAILED or not forecast.series:
        return None, [
            "no cached wave forecast at this point; wave advisories NOT derived "
            "and must not be reported as clear"
        ]

    advisories, notes = derive_wave_advisories(
        forecast.series, district or f"{lat:.2f}N {lon:.2f}E"
    )
    return advisories, notes


def active_alerts(args: ActiveAlertsIn) -> ActiveAlertsOut:
    """Advisories in force. Returns FAILED when nothing could be checked.

    The distinction this function exists to protect, restated because it is the
    most important one in the codebase:

    * ``count=0`` with ``checked=True`` means **we looked and there is nothing
      in force.** That licenses the negative finding "no cyclone is active".
    * ``status=FAILED`` with ``checked=False`` means **we could not look.**
      ``compute_risk_score`` forces ``no_go`` on it.

    Conflating the two would let a system with no feed tell a fisherman there
    is no cyclone.
    """
    requested = [t for t in args.types if t in ALERT_TYPES]
    unknown = [t for t in args.types if t not in ALERT_TYPES]
    notes: list[str] = []
    if unknown:
        notes.append(f"ignored unrecognised alert type(s): {sorted(unknown)}")

    collected: list[Advisory] = []
    live_sources: list[str] = []
    source_urls: list[str] = []

    # -- cyclones, from GDACS ------------------------------------------
    if "cyclone" in requested:
        cached = gdacs.load_cached()
        if cached is None:
            notes.append(
                "no recent GDACS cache; run scripts/refresh_cache.py --alerts. "
                "Cyclone status is UNKNOWN, not clear."
            )
        else:
            advisories, fetched_at = cached
            live_sources.append("gdacs")
            for raw in advisories:
                if not within_validity(raw.get("issued_at"), raw.get("valid_until")):
                    continue
                collected.append(
                    Advisory(
                        type="cyclone",
                        severity=str(raw["severity"]),
                        zone=str(raw.get("zone", "")),
                        issued_at=raw.get("issued_at"),
                        valid_until=raw.get("valid_until"),
                        authority=str(raw.get("authority", "GDACS")),
                        text=str(raw.get("text", "")),
                    )
                )
            if advisories and advisories[0].get("_url"):
                source_urls.append(str(advisories[0]["_url"]))
            notes.append(
                f"GDACS cache from {fetched_at.isoformat(timespec='minutes')}"
            )

    # -- wave advisories, derived from the cached forecast --------------
    #
    # Wave coverage used to depend entirely on a human editing a YAML file,
    # and the table stops counting as a check after a day. So after
    # twenty-four hours of nobody looking, every answer quietly became
    # cyclone-only. This derives the wave half from the forecast already in
    # the cache against the INCOIS criteria already in the thresholds file --
    # arithmetic on published numbers, inventing nothing. See alerts/derived.
    wave_types = {"high_wave", "swell_surge"} & set(requested)
    if wave_types and args.lat is not None and args.lon is not None:
        derived, derive_notes = _derive_wave(args.lat, args.lon, args.district)
        notes.extend(derive_notes)
        if derived is not None:
            live_sources.append("derived_wave")
            for raw in derived:
                if raw["type"] in wave_types:
                    collected.append(Advisory(**raw))

    # -- everything else, from the operator table ----------------------
    table, reviewed, table_notes = _load_operator_table()
    notes.extend(table_notes)
    if reviewed is not None:
        live_sources.append("operator_table")
        collected.extend(a for a in table if a.type in requested)
        notes.append(
            f"operator table reviewed {reviewed.isoformat(timespec='minutes')}"
        )

    checked = _checked_types(requested, live_sources)
    gaps = coverage_gaps(requested, checked)
    if gaps:
        notes.append(
            f"no source covers {sorted(gaps)}; not reported as clear"
        )

    provenance = Provenance(
        source="+".join(live_sources) if live_sources else "none",
        source_url=source_urls[0] if source_urls else None,
        retrieved_at=datetime.now(timezone.utc),
        authority=(
            "GDACS (cyclone); INCOIS criteria applied to the cached forecast "
            "(wave); INCOIS via operator table where transcribed"
        ),
    )

    # Nothing could be interrogated at all.
    if not checked:
        return ActiveAlertsOut(
            provenance=provenance,
            quality=DataQuality(notes=notes),
            status=ToolStatus.FAILED,
            error=(
                "No alert source could be checked for "
                f"{sorted(requested)}. Sources reachable: {live_sources or 'none'}. "
                "Returning FAILED rather than an empty list, because 'we could "
                "not check' is not 'there is nothing there'."
            ),
            count=0,
            alerts=[],
            checked_types=[],
            checked=False,
        )

    # De-duplicate by (type, zone), keeping the worst severity. GDACS and a
    # transcribed IMD bulletin can describe the same storm; reporting it twice
    # would double-count it in any consumer that reads `count`.
    from alerts.derived import DERIVED_AUTHORITY

    def _rank(advisory: Advisory) -> tuple[int, int]:
        # Severity first, then who said it. A human who read the actual
        # bulletin outranks our arithmetic even when the two agree, and
        # especially when they disagree -- the authority is the authority.
        return (
            SEVERITY_ORDER.index(advisory.severity),
            0 if advisory.authority == DERIVED_AUTHORITY else 1,
        )

    worst: dict[tuple[str, str], Advisory] = {}
    for advisory in collected:
        key = (advisory.type, advisory.zone)
        incumbent = worst.get(key)
        if incumbent is None or _rank(advisory) > _rank(incumbent):
            worst[key] = advisory
    alerts = sorted(
        worst.values(), key=lambda a: SEVERITY_ORDER.index(a.severity), reverse=True
    )

    # Partial coverage is a degraded check, not a clean one. `checked` stays
    # True for what was genuinely interrogated; the caveat carries the rest.
    partial = bool(gaps)
    return ActiveAlertsOut(
        provenance=provenance,
        quality=DataQuality(notes=notes),
        status=ToolStatus.DEGRADED if partial else ToolStatus.OK,
        count=len(alerts),
        alerts=alerts,
        checked_types=checked,
        checked=True,
    )


registry.implement("active_alerts")(active_alerts)
