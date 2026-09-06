"""Per-pixel, per-month chlorophyll climatology. The baseline anomalies need.

From CLAUDE.md: *10 years of monthly SST and chlorophyll reduced to per-pixel
per-month mean and std. Keep only the reduced arrays.* This module builds that
reduction and nothing else -- the monthly fields themselves are fetched,
folded in, and discarded.

**Why a climatology is not optional.** "Chlorophyll is 0.4 mg/m3" means nothing
on its own. "0.4 against a September mean of 1.1, which is 1.8 standard
deviations low" is a testable statement, and ``causal_explain`` is
unanswerable without it. The anomaly is the whole product.

**Sensor choice, and why it is not the obvious one.** The near-real-time layer
in ``ingest/sources/erddap.py`` is VIIRS. The longest monthly ocean-colour
record on NOAA CoastWatch is MODIS Aqua (``erdMH1chlamday``, 2003-2022, 19
years), and taking it would have been the natural move. It is the wrong one:
differencing a VIIRS observation against a MODIS baseline measures the offset
between two radiometers as much as it measures this year's bloom. Ocean-colour
inter-sensor bias is real and of the same order as the anomalies we are trying
to detect.

So the baseline is ``nesdisVHNSQchlaMonthly`` -- VIIRS Science Quality, global,
4 km, 2012 to present. Fourteen years, and the same instrument family as the
near-real-time product, so an anomaly measures the water rather than the
radiometer.

Two regional VIIRS monthly products were tried first and rejected on coverage:
``erdVHNchlamday`` is North Pacific only (lon -180 to -110) and ``erdMBchlamday``
is Pacific (lon 120 to 320). Both return an HTTP 404 reading "your query
produced no matching results" for the Bay of Bengal, which looks exactly like a
malformed request and is not. Check ``geospatial_lon_min`` before assuming a
CoastWatch dataset is global.

Rebuild a month with::

    python -m ingest.climatology --month 9

Idempotent. Re-running overwrites the reduced file cleanly.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ingest.sources.erddap import SERVERS, ErddapError, _get_json

__all__ = [
    "CLIMATOLOGY_DIR",
    "CHL_MONTHLY",
    "MonthlyClimatology",
    "build_month",
    "load_month",
]

CLIMATOLOGY_DIR = Path(__file__).resolve().parents[1] / "data" / "climatology"

#: The baseline source. See the module docstring for why this is VIIRS rather
#: than the longer MODIS record.
CHL_MONTHLY = {
    "server": "coastwatch",
    "dataset_id": "nesdisVHNSQchlaMonthly",
    "variable": "chlor_a",
    "units": "mg m^-3",
    "resolution_deg": 0.0375,
    # 0.0375 deg is ~4 km. Stride 2 lands near 0.075 deg, close to the 1/12 deg
    # grid the near-real-time product uses, and keeps a month's pull for this
    # box in the low thousands of cells rather than the tens of thousands.
    "stride": 2,
    "lat_ascending": False,
    "has_altitude": True,
    "authority": "NOAA CoastWatch / VIIRS",
}

#: Below this, a pixel's mean and std are not published. Three years is not a
#: climatology; reporting a sigma from it would dress noise as signal.
MIN_YEARS = 5


@dataclass(frozen=True)
class MonthlyClimatology:
    """Reduced per-pixel statistics for one calendar month."""

    dataset_id: str
    variable: str
    units: str
    month: int
    years: list[int]
    built_at: datetime
    #: (lat, lon) -> (mean, std, n_years)
    cells: dict[tuple[float, float], tuple[float, float, int]]

    def at(self, lat: float, lon: float, max_km: float = 15.0) -> tuple[float, float, int] | None:
        """Nearest cell to a point, or None if nothing is within ``max_km``.

        The distance guard matters: without it a coastal query with no valid
        baseline pixel nearby would silently borrow one 80 km offshore and
        report a confident anomaly against the wrong water.
        """
        if not self.cells:
            return None
        best_key = min(
            self.cells,
            key=lambda c: (c[0] - lat) ** 2 + ((c[1] - lon) * math.cos(math.radians(lat))) ** 2,
        )
        dlat = (best_key[0] - lat) * 111.19
        dlon = (best_key[1] - lon) * 111.19 * math.cos(math.radians(lat))
        if math.hypot(dlat, dlon) > max_km:
            return None
        return self.cells[best_key]

    def mean_within_km(
        self, lat: float, lon: float, radius_km: float
    ) -> tuple[float, float, int] | None:
        """Baseline aggregated over a disc, matching how an observation is read.

        **Use this, not :meth:`at`, whenever the observed value is itself an
        area aggregate.** Comparing a 25 km observed mean against a single
        nearest baseline pixel compares two different quantities, and near a
        turbid coastline the error is not subtle: at Point Calimere the nearest
        baseline pixel is estuarine water averaging 52 mg/m3, while the disc
        around it is mostly cleaner offshore water. Differencing the two
        produced -8.1 sigma, which is not a bloom failure, it is a units-style
        mistake wearing a plausible number.

        Returns (mean, pooled_std, n_pixels), or None if the disc holds nothing.
        The pooled standard deviation combines each pixel's interannual
        variance with the spatial variance between pixels, because both are
        real sources of spread in "what does September normally look like here".
        """
        import statistics

        inside = []
        for (clat, clon), (mean, std, n_years) in self.cells.items():
            dlat = (clat - lat) * 111.19
            dlon = (clon - lon) * 111.19 * math.cos(math.radians(lat))
            if math.hypot(dlat, dlon) <= radius_km:
                inside.append((mean, std, n_years))
        if not inside:
            return None

        means = [m for m, _, _ in inside]
        grand_mean = statistics.fmean(means)
        within = statistics.fmean([s ** 2 for _, s, _ in inside])
        between = statistics.pvariance(means) if len(means) > 1 else 0.0
        return grand_mean, math.sqrt(within + between), len(inside)

    def to_record(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "variable": self.variable,
            "units": self.units,
            "month": self.month,
            "years": self.years,
            "built_at": self.built_at.isoformat(),
            "cells": [
                [lat, lon, mean, std, n] for (lat, lon), (mean, std, n) in self.cells.items()
            ],
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "MonthlyClimatology":
        return cls(
            dataset_id=record["dataset_id"],
            variable=record["variable"],
            units=record["units"],
            month=record["month"],
            years=record["years"],
            built_at=datetime.fromisoformat(record["built_at"]),
            cells={(c[0], c[1]): (c[2], c[3], int(c[4])) for c in record["cells"]},
        )


def path_for(month: int) -> Path:
    return CLIMATOLOGY_DIR / f"chl_month_{month:02d}.json"


def _available_times(spec: dict[str, Any]) -> list[str]:
    """Every timestamp on the dataset's time axis.

    Read rather than generated. A monthly product is stamped mid-month, and
    which day varies, so constructing "2019-09-16T00:00:00Z" by hand works
    until the one year it does not.
    """
    server = SERVERS[spec["server"]]
    url = f"{server}/griddap/{spec['dataset_id']}.json?time"
    payload = _get_json(url)
    return [str(row[0]) for row in payload["table"]["rows"]]


def _fetch_month_field(
    spec: dict[str, Any], time_iso: str, bbox: tuple[float, float, float, float]
) -> dict[tuple[float, float], float]:
    west, south, east, north = bbox
    stride = spec["stride"]
    lat_part = (
        f"[({south}):{stride}:({north})]"
        if spec["lat_ascending"]
        else f"[({north}):{stride}:({south})]"
    )
    altitude = "[(0.0)]" if spec["has_altitude"] else ""
    selector = (
        f"{spec['variable']}[({time_iso})]{altitude}{lat_part}"
        f"[({west}):{stride}:({east})]"
    )
    import urllib.parse

    url = (
        f"{SERVERS[spec['server']]}/griddap/{spec['dataset_id']}.json"
        f"?{urllib.parse.quote(selector, safe='')}"
    )
    payload = _get_json(url)
    table = payload["table"]
    columns = table["columnNames"]
    lat_i, lon_i = columns.index("latitude"), columns.index("longitude")
    val_i = len(columns) - 1
    return {
        (round(float(r[lat_i]), 4), round(float(r[lon_i]), 4)): float(r[val_i])
        for r in table["rows"]
        if r[val_i] is not None
    }


def build_month(
    month: int,
    bbox: tuple[float, float, float, float],
    spec: dict[str, Any] = CHL_MONTHLY,
) -> MonthlyClimatology:
    """Fold every available year of one calendar month into mean/std per pixel.

    Years that fail to fetch are skipped rather than aborting the build; the
    surviving ``n_years`` per pixel is recorded, so a thin baseline is visible
    instead of implied.
    """
    times = [t for t in _available_times(spec) if int(t[5:7]) == month]
    if not times:
        raise ErddapError(f"{spec['dataset_id']} holds no month {month:02d}.")

    stacks: dict[tuple[float, float], list[float]] = {}
    years: list[int] = []
    for time_iso in times:
        try:
            field = _fetch_month_field(spec, time_iso, bbox)
        except ErddapError:
            continue
        if not field:
            continue
        years.append(int(time_iso[:4]))
        for key, value in field.items():
            stacks.setdefault(key, []).append(value)

    cells: dict[tuple[float, float], tuple[float, float, int]] = {}
    for key, values in stacks.items():
        if len(values) < MIN_YEARS:
            continue
        cells[key] = (
            round(statistics.fmean(values), 5),
            round(statistics.stdev(values), 5),
            len(values),
        )

    climatology = MonthlyClimatology(
        dataset_id=spec["dataset_id"],
        variable=spec["variable"],
        units=spec["units"],
        month=month,
        years=sorted(years),
        built_at=datetime.now(timezone.utc),
        cells=cells,
    )
    CLIMATOLOGY_DIR.mkdir(parents=True, exist_ok=True)
    target = path_for(month)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(climatology.to_record()), encoding="utf-8")
    tmp.replace(target)
    return climatology


def load_month(month: int) -> MonthlyClimatology | None:
    """The reduced baseline for a month, or None if it has not been built.

    None means "no baseline", which callers must report as a missing anomaly
    rather than as a zero anomaly.
    """
    target = path_for(month)
    if not target.exists():
        return None
    try:
        return MonthlyClimatology.from_record(json.loads(target.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError):
        return None


def main() -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from core import config

    parser = argparse.ArgumentParser(description="Build the chlorophyll climatology.")
    parser.add_argument(
        "--month",
        type=int,
        default=datetime.now(timezone.utc).month,
        choices=range(1, 13),
        help="Calendar month. Defaults to the current one.",
    )
    parser.add_argument("--all", action="store_true", help="Build all twelve months.")
    args = parser.parse_args()

    months = range(1, 13) if args.all else [args.month]
    for month in months:
        print(f"[climatology] month {month:02d} from {CHL_MONTHLY['dataset_id']} ...")
        climatology = build_month(month, config.bbox())
        n = len(climatology.cells)
        if n:
            means = [v[0] for v in climatology.cells.values()]
            print(
                f"  {n} pixels from {len(climatology.years)} years "
                f"{climatology.years[0]}-{climatology.years[-1]}; "
                f"mean chl {min(means):.3f}..{max(means):.3f} {climatology.units}"
            )
        else:
            print(f"  no pixel reached the {MIN_YEARS}-year minimum")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
