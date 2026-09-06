"""Route grid and A* optimisation.

Skipped without a bathymetry cache, because depth is a hard constraint that is
never assumed -- the same reason the tools themselves fail rather than guess.
"""

from __future__ import annotations

import pytest

from core import config
from core.schemas.tool_io import GeoPoint
from ingest.static import bathymetry as bathy
from tools.geo.route_grid import MIN_DEPTH_M, RouteGridIn, route_grid
from tools.risk.route_optimise import OptimiseRouteIn, optimise_route


@pytest.fixture(scope="module")
def grid():
    if bathy.load_cached() is None:
        pytest.skip("no bathymetry cache; run scripts/refresh_cache.py --static")
    out = route_grid(RouteGridIn(bbox=config.bbox(), vessel_class="frp_9m"))
    assert out.status.value == "ok"
    return out


def test_route_grid_fails_without_bathymetry_rather_than_assuming_depth(monkeypatch):
    """An unknown seabed is not a deep one."""
    monkeypatch.setattr(bathy, "load_cached", lambda: None)
    out = route_grid(RouteGridIn(bbox=config.bbox(), vessel_class="frp_9m"))
    assert out.status.value == "failed"
    assert "never assumed" in out.error


def test_land_and_shallows_are_impassable(grid):
    """A third of the box is land or too shallow; that must be excluded."""
    assert 0.0 < grid.impassable_fraction < 1.0


def test_a_deeper_vessel_gets_a_more_restricted_grid(grid):
    """A trawler needs more water than an FRP boat, so fewer cells are open."""
    trawler = route_grid(RouteGridIn(bbox=config.bbox(), vessel_class="mechanised_trawler"))
    assert MIN_DEPTH_M["mechanised_trawler"] > MIN_DEPTH_M["frp_9m"]
    assert trawler.impassable_fraction > grid.impassable_fraction


def test_a_route_is_found_and_is_costed_in_real_units(grid):
    out = optimise_route(
        OptimiseRouteIn(
            start=GeoPoint(lat=10.77, lon=79.84),
            end=GeoPoint(lat=10.29, lon=79.85),
            grid_ref=grid.grid_ref,
            vessel_class="frp_9m",
        )
    )
    assert out.status.value == "ok"
    assert out.distance_km > 0
    assert out.estimated_hours > 0
    assert len(out.waypoints) >= 2
    # Straight-line is ~53 km; a route round the shallows must not be shorter.
    assert out.distance_km >= 50.0


def test_an_unknown_grid_ref_fails_rather_than_rebuilding_silently(grid):
    out = optimise_route(
        OptimiseRouteIn(
            start=GeoPoint(lat=10.77, lon=79.84),
            end=GeoPoint(lat=10.29, lon=79.85),
            grid_ref="grid_that_does_not_exist",
            vessel_class="frp_9m",
        )
    )
    assert out.status.value == "failed"
    assert "route_grid()" in out.error


def test_the_route_reports_which_zones_it_avoided(grid):
    out = optimise_route(
        OptimiseRouteIn(
            start=GeoPoint(lat=10.77, lon=79.84),
            end=GeoPoint(lat=9.29, lon=79.31),
            grid_ref=grid.grid_ref,
            vessel_class="frp_9m",
        )
    )
    if out.status.value == "failed":
        pytest.skip(f"no route across Palk Bay for this vessel: {out.error}")
    assert "imbl" in out.avoids
