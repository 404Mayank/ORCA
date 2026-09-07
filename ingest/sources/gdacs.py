"""GDACS ingest: tropical cyclone alerts, keyless.

**This is the feed that lets `safety_assess` reach a `go` verdict at all.**

Until 2026-09-06 `active_alerts` had no source and returned FAILED by design,
which forced every safety verdict to `no_go` no matter how calm the sea was.
Correct, and useless. GDACS closes it for cyclones.

GDACS is the Global Disaster Alert and Coordination System, run by the European
Commission's Joint Research Centre with UN OCHA. Its tropical cyclone layer
aggregates JTWC and the regional specialised centres. Verified live on
2026-09-06; the API is open, keyless, and returns GeoJSON.

**What it is not.** It is not IMD. Where IMD has issued a bulletin, IMD is the
authority and this is corroboration -- so ``authority`` on every advisory built
here says GDACS, and ``alerts/rules.py`` records that GDACS is entitled to
speak to cyclones and to nothing else. High wave and swell surge are INCOIS
products with no machine-readable feed; they come from the operator table.

IMD's own endpoints were tried first and are authenticated:
``mausam.imd.gov.in/api/warnings_district_api.php`` and the nowcast API both
return **HTTP 401**. If a key is obtained later, an ``ingest/sources/imd.py``
alongside this module is the place for it, and it should take precedence over
GDACS rather than replacing it.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alerts.rules import is_regionally_relevant, normalise_severity

__all__ = [
    "CACHE_DIR",
    "EVENT_LIST_URL",
    "GdacsError",
    "fetch_cyclones",
    "load_cached",
    "refresh",
]

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "gdacs"
EVENT_LIST_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"

_TIMEOUT_S = 45.0

#: How far back to look. A cyclone that formed nine days ago and is making
#: landfall tomorrow must still be found, and GDACS dates an event from its
#: formation, not from its current episode.
LOOKBACK_DAYS = 10


class GdacsError(RuntimeError):
    """A fetch failed. Raised in ingest, never during a query."""


def _context() -> ssl.SSLContext:
    # gdacs.org has presented an incomplete chain intermittently. Public
    # unauthenticated read of a published alert list; no credential is sent.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "ORCA/0.1 (SIH 26176)"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GdacsError(f"HTTP {exc.code} from {url}") from exc
    except Exception as exc:  # noqa: BLE001 -- re-raised as our own type
        raise GdacsError(f"{type(exc).__name__} from {url}: {exc}") from exc


def _parse_date(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fetch_cyclones(
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    write_cache: bool = True,
) -> list[dict[str, Any]]:
    """Cyclone advisories relevant to the South Coromandel coast.

    Returns dicts in the normalised shape from CLAUDE.md. An empty list is a
    real answer -- "GDACS was reachable and nothing is in force" -- and the
    caller is entitled to treat it as a negative finding. A failure raises.

    ``write_cache=False`` for **historical** queries. The cache file is keyed by
    today's date, so a replay fetch for November 2018 would otherwise overwrite
    the live cyclone picture with Cyclone Gaja and leave the running system
    believing a 2018 storm was current. Expired advisories are filtered on read,
    so the verdict survived it -- but a cache that lies is a cache that will
    eventually be believed.
    """
    now = now or datetime.now(timezone.utc)
    params = {
        "eventlist": "TC",
        "fromdate": (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d"),
        "todate": (now + timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    url = f"{EVENT_LIST_URL}?{urllib.parse.urlencode(params)}"
    payload = _get(url)

    advisories: list[dict[str, Any]] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties", {}) or {}
        geometry = feature.get("geometry", {}) or {}
        coordinates = geometry.get("coordinates") or [None, None]
        lon, lat = (coordinates + [None, None])[:2]

        iso3 = [
            entry.get("iso3", "")
            for entry in (properties.get("affectedcountries") or [])
            if isinstance(entry, dict)
        ]
        if properties.get("iso3"):
            iso3.append(str(properties["iso3"]))

        if not is_regionally_relevant(lon, lat, iso3):
            continue

        severity_data = properties.get("severitydata") or {}
        advisories.append(
            {
                "type": "cyclone",
                "severity": normalise_severity(properties.get("alertlevel"), "gdacs"),
                "zone": ", ".join(
                    entry.get("countryname", "")
                    for entry in (properties.get("affectedcountries") or [])
                    if isinstance(entry, dict)
                )
                or str(properties.get("country") or "Bay of Bengal"),
                "issued_at": _parse_date(properties.get("fromdate")),
                "valid_until": _parse_date(properties.get("todate")),
                "authority": "GDACS",
                "text": (
                    f"{properties.get('eventname') or 'Tropical cyclone'}: "
                    f"{severity_data.get('severitytext') or properties.get('description') or 'tracked event'}"
                    f" (GDACS {properties.get('alertlevel')}, source {properties.get('source') or 'unknown'})"
                ),
                # Kept for provenance and for the replay, not read by the tool.
                "_event_id": properties.get("eventid"),
                "_lon": lon,
                "_lat": lat,
                "_url": url,
            }
        )
    if write_cache:
        _write_cache(advisories, url)
    return advisories


def _write_cache(advisories: list[dict[str, Any]], url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "advisories": [
            {
                k: (v.isoformat() if isinstance(v, datetime) else v)
                for k, v in advisory.items()
            }
            for advisory in advisories
        ],
    }
    path = CACHE_DIR / f"cyclones_{datetime.now(timezone.utc):%Y%m%d}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)
    return path


def load_cached(max_age_hours: float = 12.0) -> tuple[list[dict[str, Any]], datetime] | None:
    """Today's cached advisories, or None if there are none recent enough.

    **None means "not checked", never "nothing in force".** The caller must
    keep that distinction: an expired cyclone cache is exactly the situation in
    which a confident "no cyclone" would be most dangerous.

    ``max_age_hours`` is tighter than the satellite layers' tolerance on
    purpose. A twelve-hour-old chlorophyll field is fine; a twelve-hour-old
    cyclone picture is the outer limit of useful.
    """
    if not CACHE_DIR.exists():
        return None
    candidates = sorted(CACHE_DIR.glob("cyclones_*.json"), reverse=True)
    if not candidates:
        return None
    try:
        record = json.loads(candidates[0].read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(record["fetched_at"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    if (datetime.now(timezone.utc) - fetched_at) > timedelta(hours=max_age_hours):
        return None

    advisories = []
    for raw in record.get("advisories", []):
        advisory = dict(raw)
        for field in ("issued_at", "valid_until"):
            if advisory.get(field):
                advisory[field] = datetime.fromisoformat(advisory[field])
        advisories.append(advisory)
    return advisories, fetched_at


def refresh() -> list[str]:
    """Refresh the cyclone cache. Idempotent. Problems returned, not raised."""
    try:
        fetch_cyclones()
    except GdacsError as exc:
        return [f"gdacs: {exc}"]
    return []
