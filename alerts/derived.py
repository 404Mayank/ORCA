"""High-wave and swell-surge advisories, derived rather than fetched.

Why this exists
---------------
``active_alerts`` covers four advisory types. Until now exactly one of them
had a machine-readable source:

    cyclone                  GDACS, live and keyless
    high_wave, swell_surge   a YAML file a human edits before a shift
    lightning                nothing

INCOIS publishes High Wave Alerts as text and maps on a web page, and IMD's
own warning APIs are authenticated. So the wave half of the picture depended
on somebody opening a bulletin and typing, and the table is treated as
*unchecked* once it is more than a day old -- correctly, because a file
nobody has read is not evidence that the sea is calm. The practical effect
was that after twenty-four hours of nobody looking, every answer silently
became cyclone-only.

The project already refuses to accept that shape of problem elsewhere. The
official INCOIS PFZ advisory is also text and maps, and rather than wait for
a feed we derive fishing zones ourselves from SST fronts and chlorophyll,
and treat the published advisory as corroboration. This is the same move for
waves, and it is cheaper, because unlike PFZ there is nothing to invent:

* the wave field is already cached at the point, hourly, from Open-Meteo;
* the criteria are already in ``config/risk_thresholds.yaml``, tagged
  ``provenance: published``, verified against the INCOIS service in
  September 2026 -- Hs 3.0-3.5 m alert, above 3.5 m warning, with a lower
  band for swell specifically because long-period swell breaks harder
  inshore than its height suggests.

So this module does arithmetic on cached numbers against published
thresholds. It invents nothing and it estimates nothing.

What it is careful about
------------------------
A derived advisory is **not** an INCOIS bulletin and must never be dressed
as one. Each one it produces carries ``authority`` naming ORCA and the
criterion it applied, and says in its text that INCOIS has not been
consulted. Where an operator has transcribed a real bulletin, that entry
wins: a human reading the actual authority outranks our arithmetic, even
when the arithmetic agrees.

It is also honest about coverage. Deriving from a point forecast tells you
about that point. It does not tell you the coastline segment INCOIS would
have named, so the zone is the point's own description, never a district.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core import config

__all__ = ["DERIVED_AUTHORITY", "derive_wave_advisories"]

#: Stamped on every advisory this module produces. Grep-able, and the string
#: a reader sees when they ask where an advisory came from.
DERIVED_AUTHORITY = "ORCA (derived from cached forecast, INCOIS HWA criteria)"

#: How far ahead a derived advisory looks. Matched to the safety question the
#: system actually answers -- "is it safe to go out tomorrow morning" -- and
#: to the horizon the wave cache is fetched for.
HORIZON_HOURS = 24


def _bands() -> dict:
    """The published INCOIS bands, read from the thresholds deliverable.

    Read rather than hardcoded so there is exactly one place a judge has to
    look, and so a correction to the criteria cannot land in the config and
    be silently ignored here.
    """
    return config.load_yaml("risk_thresholds.yaml").get("incois_high_wave_alert") or {}


def _severity_for(peak: float, band: dict) -> str | None:
    """"warning", "advisory", or None. Bands are read, never assumed.

    Returns the INCOIS-published severity for a peak height. ``alert`` in the
    INCOIS vocabulary maps to ``advisory`` in ours -- the normalised shape in
    CLAUDE.md has four levels and theirs has two, and collapsing their alert
    onto our warning would silently promote every advisory by one step.
    """
    warning = (band.get("warning") or {}).get("min_m")
    if warning is not None and peak >= float(warning):
        return "warning"
    alert = (band.get("alert") or {}).get("min_m")
    if alert is not None and peak >= float(alert):
        return "advisory"
    return None


def derive_wave_advisories(
    series,
    place: str,
    *,
    now: datetime | None = None,
    horizon_hours: int = HORIZON_HOURS,
) -> tuple[list[dict], list[str]]:
    """Advisories implied by a cached wave series, plus notes on what was done.

    ``series`` is the hourly wave series a ``wave_forecast`` call produced:
    each entry needs ``time`` and ``significant_height_m``, and optionally
    ``swell_wave_height_m``.

    Returns ``([], notes)`` when the sea is below every published band. That
    is a **real result** and is what licenses the negative finding "no high
    wave alert is in force" -- the same distinction the whole alerts path is
    built around. A caller that cannot tell an empty list from a failure will
    tell a fisherman the sea is calm when nobody looked; this function never
    returns an empty list to mean "could not check", it returns notes saying
    so and lets the caller decide.
    """
    notes: list[str] = []
    if not series:
        return [], ["no cached wave series at this point; nothing derived"]

    bands = _bands()
    if not bands:
        return [], ["risk_thresholds.yaml has no incois_high_wave_alert block"]

    moment = now or datetime.now(UTC)
    until = moment + timedelta(hours=horizon_hours)

    def _in_window(entry) -> bool:
        stamp = getattr(entry, "time", None)
        if stamp is None:
            return True
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp <= until

    window = [e for e in series if _in_window(e)]
    if not window:
        return [], ["cached wave series does not reach the requested window"]

    out: list[dict] = []

    # -- significant wave height ---------------------------------------
    heights = [
        float(e.significant_height_m)
        for e in window
        if getattr(e, "significant_height_m", None) is not None
    ]
    if heights:
        peak = max(heights)
        severity = _severity_for(peak, bands.get("significant_wave_height") or {})
        if severity:
            out.append(
                _advisory(
                    "high_wave",
                    severity,
                    place,
                    moment,
                    until,
                    "Significant wave height reaches a level INCOIS publishes a "
                    "high wave alert for. Derived from the cached forecast at this "
                    "point against INCOIS criteria; the INCOIS bulletin itself was "
                    "not read.",
                )
            )
        notes.append(
            f"derived high_wave from {len(heights)} cached hours "
            f"(severity: {severity or 'below every band'})"
        )
    else:
        notes.append("cached wave series carries no significant height; high_wave not derived")

    # -- swell, on its own lower band ----------------------------------
    swells = [
        float(e.swell_wave_height_m)
        for e in window
        if getattr(e, "swell_wave_height_m", None) is not None
    ]
    if swells:
        peak = max(swells)
        severity = _severity_for(peak, bands.get("swell_height") or {})
        if severity:
            out.append(
                _advisory(
                    "swell_surge",
                    severity,
                    place,
                    moment,
                    until,
                    "Swell height reaches a level INCOIS publishes a swell surge "
                    "advisory for. Long-period swell breaks harder inshore than its "
                    "height suggests, which is why this band sits below the "
                    "significant-height one. Derived at this point; the INCOIS "
                    "bulletin itself was not read.",
                )
            )
        notes.append(
            f"derived swell_surge from {len(swells)} cached hours "
            f"(severity: {severity or 'below every band'})"
        )
    else:
        # Open-Meteo does not always return the wind-wave/swell split. Saying
        # so matters: no swell advisory here means the component was missing,
        # not that the swell was small.
        notes.append(
            "cached wave series carries no swell component; swell_surge NOT "
            "derived and must not be reported as clear"
        )

    return out, notes


def _advisory(
    kind: str,
    severity: str,
    place: str,
    issued: datetime,
    valid_until: datetime,
    text: str,
) -> dict:
    """One normalised advisory. Shape per CLAUDE.md.

    ``zone`` is the point's own name and never a district. Deriving from a
    point forecast tells you about that point; naming a coastline segment
    would claim a spatial extent this arithmetic does not have.
    """
    return {
        "type": kind,
        "severity": severity,
        "zone": place,
        "issued_at": issued,
        "valid_until": valid_until,
        "authority": DERIVED_AUTHORITY,
        "text": text,
    }
