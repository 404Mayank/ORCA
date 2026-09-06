"""Route optimisation over the cost grid built by route_grid()."""

from __future__ import annotations

from pydantic import Field

from core.schemas.tool_io import GeoPoint, ToolInput, ToolOutput
from tools.registry import AgentGroup, register


class OptimiseRouteIn(ToolInput):
    start: GeoPoint
    end: GeoPoint
    grid_ref: str = Field(description="From route_grid().")
    vessel_class: str


class OptimiseRouteOut(ToolOutput):
    waypoints: list[GeoPoint] = Field(default_factory=list)
    distance_km: float = Field(ge=0.0)
    estimated_hours: float = Field(ge=0.0)
    max_cell_risk: float = Field(ge=0.0, le=1.0)
    avoids: list[str] = Field(default_factory=list, description="Zone ids the route was routed around.")


register(
    "optimise_route",
    description="Least-cost route between two points over the traversal grid, avoiding geofenced zones.",
    input_model=OptimiseRouteIn,
    output_model=OptimiseRouteOut,
    agent=AgentGroup.RISK,
)


# ==========================================================================
# Implementation
# ==========================================================================
#
# A* over the cost grid built by route_grid(). Hand-rolled rather than NetworkX,
# per CLAUDE.md, and because the heuristic has to be in kilometres to stay
# admissible against a cost expressed per cell.
#
# Admissibility matters and is easy to get wrong: the heuristic must never
# overestimate the true remaining cost, or A* stops being optimal and quietly
# returns a worse route than one that exists. Minimum cell cost is 1.0, so
# straight-line distance in cells is a safe lower bound.

import heapq
import math

from core import config
from core.schemas.tool_io import DataQuality, Provenance, ToolStatus
from tools import registry
from tools.geo.route_grid import _GRIDS, _LOCK

_KM_PER_DEG = 111.19

#: 8-connected, with diagonals costed at sqrt(2). Costing them at 1.0 -- the
#: classic shortcut -- makes diagonal travel 41% cheap and produces routes that
#: zigzag for no reason.
_MOVES = [
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, 1.41421), (-1, 1, 1.41421), (1, -1, 1.41421), (1, 1, 1.41421),
]


def _to_cell(grid, lat, lon):
    return (
        int(round((lat - grid["south"]) / grid["step"])),
        int(round((lon - grid["west"]) / grid["step"])),
    )


def _to_latlon(grid, cell):
    return (
        grid["south"] + cell[0] * grid["step"],
        grid["west"] + cell[1] * grid["step"],
    )


def _nearest_open(grid, cell):
    """Closest passable cell to a point.

    A landing centre sits on the coast, so its own cell is usually land and
    therefore impassable. Refusing to route from a port because the port is not
    at sea would be technically correct and useless.
    """
    if cell in grid["cells"]:
        return cell
    best, best_d = None, None
    for candidate in grid["cells"]:
        d = (candidate[0] - cell[0]) ** 2 + (candidate[1] - cell[1]) ** 2
        if best_d is None or d < best_d:
            best, best_d = candidate, d
    # Cap the snap: 10 cells is ~55 km, beyond which the request was not really
    # about this piece of coast.
    return best if best_d is not None and best_d <= 100 else None


def _simplify(points):
    """Drop collinear waypoints. A route is steered, not followed cell by cell."""
    if len(points) <= 2:
        return points
    out = [points[0]]
    for prev, cur, nxt in zip(points, points[1:], points[2:]):
        d1 = (cur[0] - prev[0], cur[1] - prev[1])
        d2 = (nxt[0] - cur[0], nxt[1] - cur[1])
        if d1 != d2:
            out.append(cur)
    out.append(points[-1])
    return out


def optimise_route(args: OptimiseRouteIn) -> OptimiseRouteOut:
    """Least-cost route over the traversal grid. Never fetches."""
    with _LOCK:
        grid = _GRIDS.get(args.grid_ref)

    fail = lambda msg: OptimiseRouteOut(  # noqa: E731
        provenance=Provenance(source="deterministic", authority="ORCA"),
        status=ToolStatus.FAILED, error=msg,
        distance_km=0.0, estimated_hours=0.0, max_cell_risk=0.0,
    )
    if grid is None:
        return fail(
            f"Unknown grid_ref {args.grid_ref!r}. Call route_grid() first; the "
            "cost surface is per-process and is not rebuilt implicitly."
        )

    start = _nearest_open(grid, _to_cell(grid, args.start.lat, args.start.lon))
    goal = _nearest_open(grid, _to_cell(grid, args.end.lat, args.end.lon))
    if start is None or goal is None:
        return fail(
            "Start or destination has no navigable water within range for this "
            "vessel class."
        )

    cells = grid["cells"]

    def heuristic(cell):
        return math.hypot(goal[0] - cell[0], goal[1] - cell[1])

    open_heap = [(heuristic(start), 0.0, start)]
    came_from: dict = {}
    best_cost = {start: 0.0}
    seen = set()

    while open_heap:
        _, cost, cell = heapq.heappop(open_heap)
        if cell in seen:
            continue
        seen.add(cell)
        if cell == goal:
            break
        for di, dj, move_cost in _MOVES:
            nxt = (cell[0] + di, cell[1] + dj)
            weight = cells.get(nxt)
            if weight is None:
                continue
            new_cost = cost + move_cost * weight
            if new_cost < best_cost.get(nxt, float("inf")):
                best_cost[nxt] = new_cost
                came_from[nxt] = cell
                heapq.heappush(open_heap, (new_cost + heuristic(nxt), new_cost, nxt))

    if goal not in best_cost:
        # A real answer: the destination is unreachable without crossing water
        # too shallow or a zone we were told to avoid.
        return fail(
            "No navigable route exists between these points for this vessel "
            "without entering water below its minimum depth or an avoided zone."
        )

    path = [goal]
    while path[-1] != start:
        path.append(came_from[path[-1]])
    path.reverse()

    coords = [_to_latlon(grid, c) for c in path]
    distance = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:]):
        km_lon = _KM_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
        distance += math.hypot((lat2 - lat1) * _KM_PER_DEG, (lon2 - lon1) * km_lon)

    speed_kn = config.cruise_speed_kn(args.vessel_class)
    hours = distance / (speed_kn * 1.852) if speed_kn else 0.0
    peak = max(cells[c] for c in path)

    waypoints = [
        GeoPoint(lat=round(lat, 4), lon=round(lon, 4))
        for lat, lon in _simplify(coords)
    ]

    return OptimiseRouteOut(
        provenance=Provenance(source="deterministic", authority="ORCA"),
        quality=DataQuality(
            notes=[
                f"A* over {len(cells)} navigable cells at {grid['step']} deg",
                f"minimum depth {grid['min_depth_m']} m (provisional)",
            ]
        ),
        waypoints=waypoints,
        distance_km=round(distance, 2),
        estimated_hours=round(hours, 2),
        # Normalised against the grid's own worst passable cell, so this is
        # "how bad is this route relative to what was available", not an
        # absolute safety score. compute_risk_score owns absolute safety.
        max_cell_risk=round(min(peak / max(max(cells.values()), 1.0), 1.0), 4),
        avoids=list(grid["avoided"]),
    )


registry.implement("optimise_route")(optimise_route)
