"""Populate the ingest cache. The scheduled job the tool layer depends on.

    python scripts/refresh_cache.py            # everything
    python scripts/refresh_cache.py --weather  # Open-Meteo only (fast)
    python scripts/refresh_cache.py --ocean    # satellite only

This is the missing half of the ingest contract. ``ingest/sources/*`` knew how
to fetch and cache; nothing ever called them, so on a fresh checkout every
weather tool returned FAILED with "no cached data". ``data/`` is gitignored --
correctly, it is derived -- which means a clone or a zip arrives with an empty
cache and no obvious way to fill it. This is that way.

Idempotent, per CLAUDE.md: re-running overwrites cleanly rather than
duplicating. Failures are collected and reported, never raised, so one dead
endpoint does not abort the run.

Run it nightly, and once before any demo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    # Model and dataset titles carry Unicode; the Windows console defaults to
    # cp1252 and raises on them.
    sys.stdout.reconfigure(encoding="utf-8")

from core import config
from ingest.sources import erddap, gdacs, open_meteo
from ingest.static import bathymetry


def weather_points() -> list[tuple[float, float]]:
    """Every reference point in bbox.yaml that sits inside the box.

    Open-Meteo is a point API, not a grid one, so the cache is keyed by point.
    The out-of-box entries (Chennai, Tuticorin, Kochi) exist to exercise the
    refusal path and must not be fetched -- caching them would let a refusal
    test silently start passing for the wrong reason.
    """
    points = config.load_yaml("bbox.yaml")["reference_points"]
    return [
        (spec["lat"], spec["lon"])
        for spec in points.values()
        if config.in_bbox(spec["lat"], spec["lon"])
    ]


def refresh_weather() -> list[str]:
    points = weather_points()
    print(f"[weather] Open-Meteo marine + forecast for {len(points)} in-box points")
    problems = open_meteo.refresh_points(points, forecast_days=3)
    for lat, lon in points:
        cached = open_meteo.load_cached("marine", lat, lon)
        mark = "ok " if cached else "MISS"
        detail = ""
        if cached:
            hours = len(cached.hourly().get("time", []))
            detail = f"{hours}h, grid cell {cached.snapped_lat:.3f},{cached.snapped_lon:.3f}"
        print(f"  {mark} {lat:.2f},{lon:.2f}  {detail}")
    return problems


def refresh_ocean() -> list[str]:
    box = config.bbox()
    print(f"[ocean] ERDDAP satellite layers over {box}")
    problems = erddap.refresh_box(box)
    for spec in (erddap.MUR_SST, erddap.CHL_GAPFILLED):
        grid = erddap.load_cached(spec["dataset_id"])
        if grid is None:
            print(f"  MISS {spec['dataset_id']}")
            continue
        values = [v[2] for v in grid.values]
        span = f"{min(values):.3f}..{max(values):.3f} {grid.units}" if values else "empty"
        print(
            f"  ok  {grid.dataset_id:44s} {grid.time[:10]} "
            f"({grid.age_days:.1f}d old) {len(grid.values)}/{grid.requested_cells} cells  {span}"
        )
    return problems


def refresh_alerts() -> list[str]:
    """Cyclone advisories from GDACS.

    High wave and swell surge have no feed and come from the operator table in
    config/active_alerts.yaml, which a human edits. Nothing to fetch for them,
    which is why this function only covers cyclones.
    """
    print("[alerts] GDACS tropical cyclone advisories")
    problems = gdacs.refresh()
    cached = gdacs.load_cached()
    if cached is None:
        print("  MISS no usable cyclone cache")
    else:
        advisories, fetched_at = cached
        print(f"  ok  {len(advisories)} advisory(ies) relevant to the region")
        for advisory in advisories:
            print(f"      {advisory['severity']:9s} {advisory['text'][:70]}")
        if not advisories:
            print("      (an empty list is a real answer: nothing is in force)")
    return problems


def refresh_static() -> list[str]:
    """Bathymetry. Static, so this is a no-op once the file exists."""
    print("[static] bathymetry (SRTM30_PLUS)")
    problems = bathymetry.refresh(config.bbox())
    grid = bathymetry.load_cached()
    if grid is None:
        print("  MISS no bathymetry; routing will fail rather than assume depth")
    else:
        depths = list(grid.cells.values())
        print(f"  ok  {len(grid.cells)} sea cells, {min(depths):.0f}-{max(depths):.0f} m")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weather", action="store_true", help="Open-Meteo only.")
    parser.add_argument("--ocean", action="store_true", help="Satellite only.")
    parser.add_argument("--alerts", action="store_true", help="Cyclone advisories only.")
    parser.add_argument("--static", action="store_true", help="Bathymetry only (fetched once).")
    args = parser.parse_args()

    everything = not (args.weather or args.ocean or args.alerts or args.static)
    problems: list[str] = []

    if everything or args.weather:
        problems += refresh_weather()
    if everything or args.ocean:
        problems += refresh_ocean()
    if everything or args.alerts:
        problems += refresh_alerts()
    if everything or args.static:
        problems += refresh_static()

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        # Partial success is still success. The tools degrade honestly on
        # whatever is missing, which is the whole point of the cache contract.
        return 1
    print("\nCache refreshed cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
