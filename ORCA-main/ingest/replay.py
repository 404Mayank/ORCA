"""Cyclone replay: run the real pipeline against a real storm.

From CLAUDE.md:

    A live demo on a calm day proves nothing. Replaying a real cyclone and
    watching the risk score go red 36 hours before landfall proves everything.

That is the gap this closes. Every live answer today is `go`, because the Bay
is calm in September, so the only way to see a `no_go` was to break the alert
source on purpose. This replays a storm that actually happened.

---------------------------------------------------------------------------
WHY NOT CYCLONE GAJA
---------------------------------------------------------------------------

CLAUDE.md names Gaja (November 2018) and ``fetch_archive()`` was written for
it. **Open-Meteo's marine archive does not reach 2018** -- probed on
2026-09-06, wave data begins somewhere between January 2021 and November 2022,
and a Gaja window returns 96 hours of nulls. ERA5 wind is available, so a
wind-only Gaja replay is possible, but a safety demo missing its heaviest
driver is a weaker demo than one with every driver present.

So the default is **Cyclone Fengal**, which made landfall near Puducherry on
2024-11-30 -- inside the study box, on this coast, with complete wave, wind and
visibility data. Gaja remains available and is honest about what it lacks.

---------------------------------------------------------------------------
HOW THE REPLAY STAYS HONEST
---------------------------------------------------------------------------

The replay does not simulate anything. It writes archived observations into the
**same cache format the live tools already read**, then runs those same tools
with an explicit ``start`` time. ``wave_forecast`` cannot tell it is looking at
2024, and that is the point: if the risk score climbs, it climbs through the
production code path, not a demo path.

Replay data is written to ``data/replay/<event>/`` and **never** to the live
cache. Writing archived data into today's cache would leave the running system
believing a 2024 storm was current -- the same mistake a historical GDACS fetch
made, recorded in PROGRESS.md.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ingest.sources import open_meteo

__all__ = ["EVENTS", "ReplayEvent", "fetch_event", "replay_cache", "available"]

REPLAY_DIR = Path(__file__).resolve().parents[1] / "data" / "replay"

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class ReplayEvent:
    """A real storm, and where to stand while it passes."""

    id: str
    name: str
    #: Where the fisherman is asking from. A reference point in bbox.yaml.
    place: str
    lat: float
    lon: float
    start: str
    end: str
    landfall: str
    vessel_class: str
    authority: str
    note: str

    @property
    def landfall_at(self) -> datetime:
        return datetime.fromisoformat(self.landfall).replace(tzinfo=IST)


EVENTS: dict[str, ReplayEvent] = {
    "fengal": ReplayEvent(
        id="fengal",
        name="Cyclone Fengal",
        place="Cuddalore",
        lat=11.75,
        lon=79.77,
        start="2024-11-28",
        end="2024-12-02",
        landfall="2024-11-30T23:30:00",
        vessel_class="frp_9m",
        authority="GDACS FENGAL-24 (Orange), IMD",
        note=(
            "Landfall near Puducherry, ~40 km north of Cuddalore. Complete "
            "wave, wind and visibility data."
        ),
    ),
    "mandous": ReplayEvent(
        id="mandous",
        name="Cyclone Mandous",
        place="Cuddalore",
        lat=11.75,
        lon=79.77,
        start="2022-12-06",
        end="2022-12-11",
        landfall="2022-12-09T23:30:00",
        vessel_class="frp_9m",
        authority="GDACS MANDOUS-22 (Orange), IMD",
        note="Crossed near Mahabalipuram. Complete data.",
    ),
    "gaja": ReplayEvent(
        id="gaja",
        name="Cyclone Gaja",
        place="Nagapattinam",
        lat=10.77,
        lon=79.84,
        start="2018-11-14",
        end="2018-11-18",
        landfall="2018-11-16T00:30:00",
        vessel_class="frp_9m",
        authority="GDACS GAJA-18 (Red), IMD",
        note=(
            "WIND ONLY. The marine archive does not reach 2018, so wave height "
            "is absent and the risk score runs on wind and visibility alone. "
            "Kept because Gaja is the storm this coast remembers."
        ),
    ),
}


def _event_dir(event: ReplayEvent) -> Path:
    return REPLAY_DIR / event.id


def _get(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "ORCA/0.1 (SIH 26176)"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_event(event: ReplayEvent) -> dict[str, int]:
    """Fetch the archive for one event into its replay cache. Idempotent.

    Returns the hour count per layer. A layer with no data -- Gaja's waves --
    is written as an empty series rather than skipped, so the tool that reads
    it fails honestly instead of finding no file and reporting a missing cache.
    """
    marine_params = {
        "latitude": event.lat,
        "longitude": event.lon,
        "hourly": ",".join(open_meteo.MARINE_HOURLY),
        "start_date": event.start,
        "end_date": event.end,
        "timezone": "Asia/Kolkata",
    }
    forecast_params = {
        "latitude": event.lat,
        "longitude": event.lon,
        "hourly": ",".join(open_meteo.FORECAST_HOURLY),
        "wind_speed_unit": "kn",
        "start_date": event.start,
        "end_date": event.end,
        "timezone": "Asia/Kolkata",
    }

    target = _event_dir(event)
    target.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    for endpoint, base, params in (
        ("marine", open_meteo.MARINE_URL, marine_params),
        ("forecast", open_meteo.ARCHIVE_URL, forecast_params),
    ):
        url = f"{base}?{urllib.parse.urlencode(params, doseq=True)}"
        payload = _get(url)
        record = {
            "endpoint": endpoint,
            "url": url,
            "params": params,
            # The retrieval time is now; the *observations* are historical. The
            # distinction matters because DataQuality.data_age_days is computed
            # from this, and a replay is not stale data -- it is archived data
            # being read deliberately.
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        path = target / open_meteo.cache_path(endpoint, event.lat, event.lon).name
        path.write_text(json.dumps(record), encoding="utf-8")

        series = payload.get("hourly", {})
        first = next((k for k in series if k != "time"), None)
        counts[endpoint] = sum(
            1 for value in (series.get(first) or []) if value is not None
        )
    return counts


def available(event: ReplayEvent) -> bool:
    directory = _event_dir(event)
    return directory.exists() and any(directory.glob("*.json"))


@contextmanager
def replay_cache(event: ReplayEvent) -> Iterator[None]:
    """Point the weather tools at an event's archive for the duration.

    The tools are untouched -- they read ``open_meteo.CACHE_DIR`` as always,
    and it simply points somewhere else while this is open. That is what makes
    a replay evidence rather than a demo: the code under test is the production
    code, not a parallel path written to look convincing.
    """
    original = open_meteo.CACHE_DIR
    open_meteo.CACHE_DIR = _event_dir(event)
    try:
        yield
    finally:
        open_meteo.CACHE_DIR = original
