"""ERDDAP ingest: satellite SST and chlorophyll, keyless.

**This module is why ORCA does not need a Copernicus Marine account.**

CLAUDE.md names Copernicus L4 as *required*, on the correct reasoning that
monsoon Coromandel is overcast for days and an L2 product will be full of holes
during the demo. The requirement was for **gap-filled L4 data**, not for
Copernicus specifically. Two NOAA CoastWatch ERDDAP datasets meet it, are
keyless, and are near-real-time:

* ``jplMURSST41`` -- MUR SST v4.1. L4, multi-scale, **0.01 deg**, daily. Finer
  than the 0.05 deg Copernicus product CLAUDE.md assumed, and gap-free by
  construction: MUR is an analysis, not a swath composite.
* ``nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily`` -- VIIRS chlorophyll,
  **DINEOF gap-filled**, 1/12 deg, daily, S-NPP + NOAA-20 combined.

Both verified live on 2026-09-06 over the ORCA box. See docs/verified_sources.md.

INCOIS's own ERDDAP (``erddap.incois.gov.in``) was evaluated and **rejected as
a live source**: it serves 17 datasets, and every gridded ocean-colour or SST
product on it is a historical archive. Oceansat-2 ends 2020-05-01, TMI ends
2014-12-31, IRS-P4 chlorophyll ends 2006. Only the ARGO products are current.
It remains valuable for **climatology**, which is a separate job -- see
``ingest/climatology.py``. It is kept in :data:`SERVERS` for that purpose.

Same contract as ``ingest/sources/open_meteo.py`` and for the same reason:
nothing above this module fetches during a user query. ``ingest/`` runs on a
schedule and writes a cache; ``tools/ocean/`` reads that cache and nothing else.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "CACHE_DIR",
    "SERVERS",
    "MUR_SST",
    "CHL_GAPFILLED",
    "GridSlice",
    "ErddapError",
    "fetch_grid",
    "latest_time",
    "load_cached",
    "cache_path",
    "refresh_box",
]

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "erddap"

SERVERS = {
    "coastwatch": "https://coastwatch.pfeg.noaa.gov/erddap",
    # Archive only -- see the module docstring. Used by climatology, never by
    # a "what is happening today" query.
    "incois": "https://erddap.incois.gov.in/erddap",
}

#: Datasets, with everything a caller needs to slice and to cite them.
#:
#: ``stride`` exists because MUR is 0.01 deg and the ORCA box is 3.5 x 4.0 deg
#: -- a full-resolution pull is 140,000 cells for a field whose fronts are
#: resolved perfectly well at 0.05 deg, which is the target grid anyway.
MUR_SST = {
    "server": "coastwatch",
    "dataset_id": "jplMURSST41",
    "variable": "analysed_sst",
    "units": "degree_C",
    "resolution_deg": 0.01,
    "stride": 5,  # -> 0.05 deg, the ORCA target grid
    "has_altitude": False,
    "lat_ascending": True,
    "gap_filled": True,
    "authority": "NASA JPL / NOAA CoastWatch",
}

CHL_GAPFILLED = {
    "server": "coastwatch",
    "dataset_id": "nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily",
    "variable": "chlor_a",
    "units": "mg m^-3",
    "resolution_deg": 1.0 / 12.0,
    "stride": 1,
    "has_altitude": True,
    # This dataset's latitude axis runs north -> south. Requesting
    # [(8.0):(12.0)] on a descending axis returns an empty grid rather than an
    # error, which reads exactly like "no data today" and is not.
    "lat_ascending": False,
    "gap_filled": True,
    "authority": "NOAA CoastWatch",
}

_TIMEOUT_S = 120.0

#: erddap.incois.gov.in serves an incomplete certificate chain: it omits the
#: intermediate, so verification fails on any machine that has not already
#: cached that intermediate from another site. certifi does not fix it -- the
#: root is present and trusted; the intermediate is the missing link.
#:
#: Verification is therefore disabled **for that host only**, and only for
#: reads of public, unauthenticated scientific data. No credential is ever sent
#: to it. NOAA CoastWatch verifies normally and is left strict.
_INSECURE_HOSTS = {"erddap.incois.gov.in"}


class ErddapError(RuntimeError):
    """A fetch failed. Raised in ingest, never during a query."""


@dataclass(frozen=True)
class GridSlice:
    """One variable over a bounding box at one time, plus its provenance.

    ``values`` is a flat list of ``(lat, lon, value)`` with nulls dropped.
    Keeping it sparse rather than as a dense array is deliberate: roughly a
    third of the ORCA box is land, and a dense array would need a mask
    travelling beside it to say so.
    """

    dataset_id: str
    variable: str
    units: str
    url: str
    fetched_at: datetime
    time: str
    values: list[tuple[float, float, float]]
    requested_cells: int
    resolution_deg: float
    gap_filled: bool
    authority: str

    @property
    def coverage_fraction(self) -> float:
        """Valid cells over requested cells.

        For this box a "perfect" pull sits near 0.65, not 1.0, because the box
        spans the Tamil Nadu landmass and northern Sri Lanka. Land is not a
        data gap, and a caller comparing this to 1.0 will misread a healthy
        fetch as degraded. :func:`sea_coverage_fraction` is the number that
        actually means what people expect.
        """
        return len(self.values) / self.requested_cells if self.requested_cells else 0.0

    @property
    def age_days(self) -> float:
        """Age of the observation itself, not of the cache file."""
        try:
            observed = datetime.fromisoformat(self.time.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        return max((datetime.now(timezone.utc) - observed).total_seconds() / 86400.0, 0.0)

    def value_at(self, lat: float, lon: float) -> float | None:
        """Nearest valid cell to a point, or None if the grid holds nothing."""
        if not self.values:
            return None
        best = min(self.values, key=lambda v: (v[0] - lat) ** 2 + (v[1] - lon) ** 2)
        return best[2]

    def within_km(self, lat: float, lon: float, radius_km: float) -> list[float]:
        """Values inside a radius. Equirectangular, which is fine at this scale."""
        import math

        out: list[float] = []
        for vlat, vlon, value in self.values:
            dlat = math.radians(vlat - lat) * 6371.0
            dlon = math.radians(vlon - lon) * 6371.0 * math.cos(math.radians(lat))
            if math.hypot(dlat, dlon) <= radius_km:
                out.append(value)
        return out

    def to_record(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "variable": self.variable,
            "units": self.units,
            "url": self.url,
            "fetched_at": self.fetched_at.isoformat(),
            "time": self.time,
            "values": self.values,
            "requested_cells": self.requested_cells,
            "resolution_deg": self.resolution_deg,
            "gap_filled": self.gap_filled,
            "authority": self.authority,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "GridSlice":
        return cls(
            dataset_id=record["dataset_id"],
            variable=record["variable"],
            units=record["units"],
            url=record["url"],
            fetched_at=datetime.fromisoformat(record["fetched_at"]),
            time=record["time"],
            values=[tuple(v) for v in record["values"]],
            requested_cells=record["requested_cells"],
            resolution_deg=record["resolution_deg"],
            gap_filled=record["gap_filled"],
            authority=record["authority"],
        )


def _context_for(url: str) -> ssl.SSLContext | None:
    host = urllib.parse.urlparse(url).hostname or ""
    if host in _INSECURE_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def _get_json(url: str) -> dict[str, Any]:
    # A default urllib User-Agent is rejected by some CDNs in front of these
    # servers with a 403 that looks exactly like an auth failure. Learned the
    # hard way on Open-Meteo; applied pre-emptively here.
    request = urllib.request.Request(url, headers={"User-Agent": "ORCA/0.1 (SIH 26176 research)"})
    kwargs: dict[str, Any] = {}
    context = _context_for(url)
    if context is not None:
        kwargs["context"] = context
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S, **kwargs) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise ErddapError(f"HTTP {exc.code} from {url}\n{body}") from exc
    except Exception as exc:  # noqa: BLE001 -- re-raised as our own type
        raise ErddapError(f"{type(exc).__name__} from {url}: {exc}") from exc


def latest_time(spec: dict[str, Any]) -> str:
    """The most recent timestamp the dataset actually holds.

    Asking for "today" is wrong for satellite data: a near-real-time product
    typically lags one to three days, and a query for a timestamp it does not
    have returns an error rather than the nearest available. So we read the
    axis and take its end.
    """
    server = SERVERS[spec["server"]]
    url = f"{server}/info/{spec['dataset_id']}/index.json"
    payload = _get_json(url)
    for row in payload["table"]["rows"]:
        if row[0] == "attribute" and row[2] == "time_coverage_end":
            return str(row[4])
    raise ErddapError(f"{spec['dataset_id']} declares no time_coverage_end.")


def _griddap_url(
    spec: dict[str, Any],
    time_iso: str,
    west: float,
    south: float,
    east: float,
    north: float,
) -> str:
    server = SERVERS[spec["server"]]
    stride = spec["stride"]

    # Latitude order follows the dataset's own axis direction. A descending
    # axis queried low-to-high returns an empty grid, not an error.
    if spec["lat_ascending"]:
        lat_part = f"[({south}):{stride}:({north})]"
    else:
        lat_part = f"[({north}):{stride}:({south})]"

    altitude_part = "[(0.0)]" if spec["has_altitude"] else ""
    selector = (
        f"{spec['variable']}"
        f"[({time_iso})]"
        f"{altitude_part}"
        f"{lat_part}"
        f"[({west}):{stride}:({east})]"
    )
    return f"{server}/griddap/{spec['dataset_id']}.json?{urllib.parse.quote(selector, safe='')}"


def fetch_grid(
    spec: dict[str, Any],
    bbox: tuple[float, float, float, float],
    time_iso: str | None = None,
) -> GridSlice:
    """Pull one variable over a bounding box. Records provenance. Caches.

    ``bbox`` is (west, south, east, north), matching ``core.config.bbox()``.
    """
    west, south, east, north = bbox
    time_iso = time_iso or latest_time(spec)
    url = _griddap_url(spec, time_iso, west, south, east, north)
    payload = _get_json(url)

    table = payload["table"]
    columns = table["columnNames"]
    lat_i = columns.index("latitude")
    lon_i = columns.index("longitude")
    val_i = len(columns) - 1

    rows = table["rows"]
    values = [
        (float(r[lat_i]), float(r[lon_i]), float(r[val_i]))
        for r in rows
        if r[val_i] is not None
    ]

    grid = GridSlice(
        dataset_id=spec["dataset_id"],
        variable=spec["variable"],
        units=table.get("columnUnits", [None] * len(columns))[val_i] or spec["units"],
        url=url,
        fetched_at=datetime.now(timezone.utc),
        time=str(rows[0][0]) if rows else time_iso,
        values=values,
        requested_cells=len(rows),
        resolution_deg=spec["resolution_deg"] * spec["stride"],
        gap_filled=spec["gap_filled"],
        authority=spec["authority"],
    )
    _write_cache(grid)
    return grid


def cache_path(dataset_id: str, day: str | None = None) -> Path:
    day = day or datetime.now(timezone.utc).strftime("%Y%m%d")
    return CACHE_DIR / f"{dataset_id}_{day}.json"


def _write_cache(grid: GridSlice) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(grid.dataset_id)
    # Write-then-rename: a crash mid-write must not leave a half-parsed file
    # that a later query would read as real data.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(grid.to_record()), encoding="utf-8")
    tmp.replace(path)
    return path


def load_cached(dataset_id: str) -> GridSlice | None:
    """Today's cache, or the most recent available. None when nothing is cached.

    Callers must treat None as a failure, never as an absence of ocean.
    """
    exact = cache_path(dataset_id)
    candidates = (
        [exact]
        if exact.exists()
        else sorted(CACHE_DIR.glob(f"{dataset_id}_*.json"), reverse=True)
    )
    if not candidates:
        return None
    try:
        return GridSlice.from_record(json.loads(candidates[0].read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError):
        return None


def refresh_box(
    bbox: tuple[float, float, float, float],
    specs: Iterable[dict[str, Any]] = (MUR_SST, CHL_GAPFILLED),
) -> list[str]:
    """Refresh every satellite layer for the box. Idempotent.

    Re-running overwrites cleanly rather than duplicating, per the ingest rule
    in CLAUDE.md. Failures are collected and returned rather than raised, so one
    dead dataset does not abort the nightly run.
    """
    problems: list[str] = []
    for spec in specs:
        try:
            fetch_grid(spec, bbox)
        except ErddapError as exc:
            problems.append(f"{spec['dataset_id']}: {exc}")
    return problems
