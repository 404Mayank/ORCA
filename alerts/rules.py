"""Alert normalisation: many authorities, one shape.

CLAUDE.md fixes the normalised form::

    {type, severity, zone, issued_at, valid_until, authority, text}

This module owns the mapping into it, and one judgement that matters more than
the mapping: **which authority is allowed to say what.**

---------------------------------------------------------------------------
THE HONESTY PROBLEM THIS MODULE EXISTS TO SOLVE
---------------------------------------------------------------------------

``active_alerts`` has to answer "is anything in force?" and the difference
between *no* and *I could not check* is the most consequential distinction in
the codebase. With several sources, that distinction becomes per-type:

* **Cyclones** are covered by GDACS (EU JRC, drawing on JTWC and regional
  centres). Live, keyless, verified.
* **High wave and swell surge** are INCOIS products published as text and maps.
  **No machine-readable feed exists.** They are covered only by the operator
  table in ``config/active_alerts.yaml``.
* **Lightning** is covered by nothing.

So "checked" is not one bit, it is a set. :func:`checked_types` returns exactly
the types that were genuinely interrogated, and ``ActiveAlertsOut.checked`` is
True only when every *requested* type is in it. Asking for cyclone alone on a
day GDACS is reachable is a complete check; asking for cyclone plus lightning
never is.

**GDACS is not IMD.** It is an international aggregator and it is recorded as
such in ``authority``. Where IMD has issued a bulletin, IMD is the authority
and GDACS is corroboration. Presenting GDACS as the Indian official warning
would be a false citation, and the audit trail is the whole product here.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

__all__ = [
    "ALERT_TYPES",
    "SEVERITY_ORDER",
    "GDACS_ALERT_TO_SEVERITY",
    "TYPE_COVERAGE",
    "normalise_severity",
    "is_regionally_relevant",
    "checked_types",
    "coverage_gaps",
]

#: The four types the system recognises. Anything else is dropped rather than
#: passed through under a name nothing downstream understands.
ALERT_TYPES = ("cyclone", "high_wave", "swell_surge", "lightning")

#: Ascending. Used to pick the worst of several overlapping advisories.
SEVERITY_ORDER = ("advisory", "watch", "warning", "severe")

#: GDACS publishes Green / Orange / Red. Mapped conservatively: Green is a real
#: event being tracked and is worth surfacing as an advisory, not discarded.
GDACS_ALERT_TO_SEVERITY = {
    "green": "advisory",
    "orange": "warning",
    "red": "severe",
}

#: Which source can speak to which alert type. The value is the set of types a
#: source is entitled to mark as checked -- not the types it happens to return.
#: A source that returns nothing for a type it covers is a negative finding; a
#: source that covers nothing for a type leaves that type unchecked.
TYPE_COVERAGE = {
    "gdacs": {"cyclone"},
    "operator_table": {"cyclone", "high_wave", "swell_surge"},
    # No source. Kept here so the gap is visible in code rather than implied by
    # absence: lightning nowcasts are an IMD product behind an authenticated
    # API (HTTP 401 as of 2026-09-06).
    "imd_lightning": set(),
}

#: The Bay of Bengal / Arabian Sea approach corridor. Wider than the ORCA box
#: on purpose: a cyclone 600 km away is not in our box and is absolutely our
#: problem, because the swell arrives long before the storm does.
RELEVANCE_BOX = (60.0, 0.0, 100.0, 30.0)  # west, south, east, north

#: Countries whose involvement makes a cyclone relevant regardless of where its
#: plotted centre currently sits. GDACS reports a track's latest position, so
#: Gaja-18 plots at 67.8E -- in the Arabian Sea, after crossing the peninsula --
#: while having made landfall in Tamil Nadu days earlier.
RELEVANT_ISO3 = {"IND", "LKA"}


def normalise_severity(raw: str | None, scheme: str = "gdacs") -> str:
    """Map a source's severity vocabulary onto :data:`SEVERITY_ORDER`."""
    if not raw:
        return "advisory"
    key = str(raw).strip().lower()
    if scheme == "gdacs":
        return GDACS_ALERT_TO_SEVERITY.get(key, "advisory")
    return key if key in SEVERITY_ORDER else "advisory"


def is_regionally_relevant(
    lon: float | None,
    lat: float | None,
    affected_iso3: Iterable[str] = (),
) -> bool:
    """Whether a cyclone bears on the South Coromandel coast.

    Two independent tests, either sufficient:

    1.  Its plotted position is inside the wide approach corridor.
    2.  India or Sri Lanka is among its affected countries.

    The second exists because the first is not sufficient on its own. GDACS
    plots a storm's *latest* position, so a cyclone that crossed Tamil Nadu and
    weakened over the Arabian Sea plots west of the subcontinent while having
    been the most important event of the month for our users. Gaja-18 is
    exactly that case and is the reason this function is not a bounding-box
    test.
    """
    if any(code.upper() in RELEVANT_ISO3 for code in affected_iso3 if code):
        return True
    if lon is None or lat is None:
        return False
    west, south, east, north = RELEVANCE_BOX
    return west <= lon <= east and south <= lat <= north


def checked_types(
    requested: Iterable[str],
    available_sources: Iterable[str],
) -> list[str]:
    """The requested types that were genuinely interrogated.

    The intersection of what was asked for with what the reachable sources are
    entitled to speak to. A type absent from the result was not checked, and
    nothing downstream may report it as clear.
    """
    covered: set[str] = set()
    for source in available_sources:
        covered |= TYPE_COVERAGE.get(source, set())
    return [t for t in requested if t in covered]


def coverage_gaps(requested: Iterable[str], checked: Iterable[str]) -> list[str]:
    """Requested types with no source behind them. For honest caveats."""
    return [t for t in requested if t not in set(checked)]


def within_validity(
    valid_from: datetime | None,
    valid_until: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Whether an advisory is in force.

    An advisory with no end time is treated as in force for
    :data:`DEFAULT_VALIDITY_HOURS` from issue, rather than forever. A stale
    warning left in the operator table would otherwise pin the verdict to
    ``no_go`` indefinitely, and an advisory system nobody can turn off is one
    people learn to ignore.
    """
    now = now or datetime.now(timezone.utc)
    if valid_from is not None and now < valid_from:
        return False
    if valid_until is not None:
        return now <= valid_until
    if valid_from is not None:
        return now <= valid_from + timedelta(hours=DEFAULT_VALIDITY_HOURS)
    return True


#: How long an advisory with no stated end time stays in force.
DEFAULT_VALIDITY_HOURS = 24
