"""Shelter-leg route: build it from the tool log, verify it, draw nothing else.

Hermetic -- no live services, no cache. Every fixture is a hand-built
ExecutionResult or ToolCallRecord, so these run the same on a plane as on
the desk.

What is pinned here, and why:

* An OK optimise_route output becomes rec.route AND passes the verifier;
  a FAILED-only log leaves route None AND still verifies (degraded answers
  must verify -- otherwise every bad-weather day is a crash).
* computed_by pointing at a FAILED call is rejected both ways (ERROR when
  failed, pass when OK).
* A waypoint shifted by 0.0002 -- inside display-rounding tolerance, so the
  flat numbers_match haystack could wave it through -- is rejected, as is a
  count mismatch. Coordinates come from the tool output verbatim.
* First-OK: FAILED-then-OK cites the OK call; OK-then-FAILED keeps the OK.
  A failed check is not a usable endpoint.
* The review floor is stateless: round 1 asks shelter + grid, round 2 (shelter
  and grid OK, no route yet) asks the leg, a FAILED grid asks nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

import agents
from agents.synthesis_agent import _build_route
from core import config
from core.provenance import ToolCallLog
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from core.schemas.recommendation import (
    EvidenceEntry,
    Recommendation,
    Templated,
)
from core.schemas.tool_io import GeoPoint, Provenance, ToolCallRecord, ToolStatus
from orchestrator.executor import ExecutionResult
from orchestrator.verifier import verify
from tools.geo.nearest import NearestLandingCentreOut, ResolvePlaceOut
from tools.geo.route_grid import RouteGridOut
from tools.risk.risk_score import ComputeRiskScoreOut
from tools.risk.route_optimise import OptimiseRouteOut

PROV = Provenance(source="deterministic", authority="ORCA")

WAYPOINTS = [(10.77, 79.84), (10.5, 79.845), (10.29, 79.85)]
DISTANCE_KM = 53.2
HOURS = 4.1


def _intent() -> Intent:
    return Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to go out?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )


def _result(*calls) -> ExecutionResult:
    """An ExecutionResult from (step_id, tool, output) triples, in order."""
    log = ToolCallLog(turn_id="t_route")
    outputs, ids = {}, {}
    for step_id, tool, output in calls:
        record = log.record(
            tool=tool,
            step_id=step_id,
            args={},
            output=output,
            started_at=datetime.now(UTC),
            duration_ms=1,
        )
        outputs[step_id] = output
        ids[step_id] = record.tool_call_id
    return ExecutionResult(log=log, outputs=outputs, call_ids=ids)


def _place():
    return ResolvePlaceOut(
        provenance=Provenance(source="bbox.yaml", authority="ORCA"),
        matched_name="Nagapattinam",
        lat=10.77,
        lon=79.84,
        in_bbox=True,
        match_confidence=0.8,
    )


def _risk(verdict: str):
    return ComputeRiskScoreOut(
        provenance=PROV,
        score=0.5,
        band="x",
        verdict=verdict,
        contributions=[],
    )


def _shelter():
    return NearestLandingCentreOut(
        provenance=PROV,
        name="Kodiakkarai",
        lat=10.29,
        lon=79.85,
        distance_km=53.0,
        bearing_deg=180.0,
    )


def _grid(grid_ref: str = "grid_abc123"):
    return RouteGridOut(
        provenance=PROV,
        n_lat=80,
        n_lon=70,
        impassable_fraction=0.4,
        max_cost=9.0,
        grid_ref=grid_ref,
    )


def _route_out(status: ToolStatus = ToolStatus.OK, error: str | None = None):
    return OptimiseRouteOut(
        provenance=PROV,
        status=status,
        error=error,
        waypoints=[GeoPoint(lat=lat, lon=lon) for lat, lon in WAYPOINTS],
        distance_km=DISTANCE_KM,
        estimated_hours=HOURS,
        max_cell_risk=0.3,
    )


def _rec_with_route(route) -> Recommendation:
    return Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id="t_route",
        headline=Templated(template="Do not go out."),
        route=route,
        evidence=[
            EvidenceEntry(
                tool_call_id=route.computed_by,
                tool="optimise_route",
                args={},
                output_digest={},
                source="deterministic",
            )
        ],
    )


def _route_log_record(
    call_id: str = "tc_009",
    status: ToolStatus = ToolStatus.OK,
    waypoints: list | None = None,
) -> ToolCallRecord:
    pts = (
        waypoints
        if waypoints is not None
        else [{"lat": lat, "lon": lon} for lat, lon in WAYPOINTS]
    )
    flat = [DISTANCE_KM, HOURS]
    for pt in pts:
        flat.extend([pt["lat"], pt["lon"]])
    return ToolCallRecord(
        tool_call_id=call_id,
        tool="optimise_route",
        step_id="s9",
        turn_id="t_route",
        args={},
        output={
            "waypoints": pts,
            "distance_km": DISTANCE_KM,
            "estimated_hours": HOURS,
        },
        output_numbers=flat,
        status=status,
        error="no navigable route" if status is ToolStatus.FAILED else None,
    )


# ==========================================================================
# Existence, both directions
# ==========================================================================


def test_ok_route_becomes_rec_route_and_verifies():
    """The happy path: tool ran, synthesis cites it, verifier agrees."""
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    assert route is not None
    assert [(p.lat, p.lon) for p in route.waypoints] == WAYPOINTS
    assert route.distance_km == DISTANCE_KM
    assert route.estimated_hours == HOURS
    assert route.destination_name == "Kodiakkarai"
    assert route.computed_by == result.call_id_for("s9")

    rec = _rec_with_route(route)
    log = {**result.tool_call_log, "tc_009": _route_log_record(call_id=route.computed_by)}
    # The builder cites the executed call id; the log record carries the same
    # id with the structured output the verifier walks.
    report = verify(rec, log)
    assert report.ok, [str(v) for v in report.violations]


def test_failed_only_route_leaves_route_none_and_still_verifies():
    """A failed optimisation is a degraded answer, not a crash and not a line."""
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out(ToolStatus.FAILED, "Unknown grid_ref")),
    )
    assert _build_route(result) is None

    rec = Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id="t_route",
        headline=Templated(template="Do not go out."),
        route=None,
        evidence=[],
    )
    assert verify(rec, result.tool_call_log).ok


def test_no_route_tools_means_no_route_and_no_crash_on_go():
    """A calm day never runs the route tools; the builder must not care."""
    result = _result(
        ("s1", "resolve_place", _place()),
        ("s6", "compute_risk_score", _risk("go")),
    )
    assert _build_route(result) is None
    requests = agents.agent_for("risk").review(result, _intent())
    assert requests == []


# ==========================================================================
# Failed-call guard, both directions
# ==========================================================================


def test_route_citing_a_failed_call_is_rejected():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    assert route is not None
    rec = _rec_with_route(route)
    log = {
        **result.tool_call_log,
        route.computed_by: _route_log_record(
            call_id=route.computed_by, status=ToolStatus.FAILED
        ),
    }
    report = verify(rec, log)
    assert not report.ok
    assert any(v.code == "route_from_failed_call" for v in report.errors)


def test_route_citing_an_ok_call_passes_the_guard():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    rec = _rec_with_route(route)
    log = {**result.tool_call_log, route.computed_by: _route_log_record(
        call_id=route.computed_by
    )}
    report = verify(rec, log)
    assert report.ok, [str(v) for v in report.violations]


def test_route_citing_a_call_that_never_ran_is_rejected():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    rec = _rec_with_route(route)
    log = dict(result.tool_call_log)
    log.pop(route.computed_by)
    report = verify(rec, log)
    assert not report.ok
    assert any(v.code == "route_from_failed_call" for v in report.errors)


# ==========================================================================
# Coordinate soundness: verbatim or rejected
# ==========================================================================


def test_hallucinated_waypoint_is_rejected():
    """One latitude shifted 0.0002 -- inside display tolerance, so the flat
    haystack would not catch it; the structured check must."""
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    assert route is not None
    tampered = route.model_copy(
        update={
            "waypoints": [
                GeoPoint(lat=lat + (0.0002 if i == 1 else 0.0), lon=lon)
                for i, (lat, lon) in enumerate(WAYPOINTS)
            ]
        }
    )
    rec = _rec_with_route(tampered)
    log = {**result.tool_call_log, route.computed_by: _route_log_record(
        call_id=route.computed_by
    )}
    report = verify(rec, log)
    assert not report.ok
    assert any(v.code == "route_waypoint_mismatch" for v in report.errors)


def test_count_mismatch_is_rejected():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    assert route is not None
    short = route.model_copy(
        update={"waypoints": [GeoPoint(lat=lat, lon=lon) for lat, lon in WAYPOINTS[:2]]}
    )
    rec = _rec_with_route(short)
    log = {**result.tool_call_log, route.computed_by: _route_log_record(
        call_id=route.computed_by
    )}
    report = verify(rec, log)
    assert not report.ok
    assert any(v.code == "route_waypoint_mismatch" for v in report.errors)


def test_dangling_route_citation_cannot_be_built():
    """Rule 3 extends to the route block: computed_by must resolve."""
    with pytest.raises(ValueError, match="route.computed_by"):
        Recommendation(
            query_type=QueryType.SAFETY_ASSESS,
            turn_id="t_route",
            headline=Templated(template="Do not go out."),
            route=_route_out_route("tc_999"),
            evidence=[
                EvidenceEntry(
                    tool_call_id="tc_001",
                    tool="resolve_place",
                    args={},
                    output_digest={},
                    source="bbox.yaml",
                )
            ],
        )


def _route_out_route(call_id: str):
    from core.schemas.recommendation import Route

    return Route(
        waypoints=[GeoPoint(lat=lat, lon=lon) for lat, lon in WAYPOINTS],
        distance_km=DISTANCE_KM,
        estimated_hours=HOURS,
        computed_by=call_id,
    )


# ==========================================================================
# First-OK semantics
# ==========================================================================


def test_failed_then_ok_cites_the_ok_call_and_verifies():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9a", "optimise_route", _route_out(ToolStatus.FAILED, "Unknown grid_ref")),
        ("s9b", "optimise_route", _route_out()),
    )
    route = _build_route(result)
    assert route is not None
    assert route.computed_by == result.call_id_for("s9b")

    rec = _rec_with_route(route)
    log = {**result.tool_call_log, route.computed_by: _route_log_record(
        call_id=route.computed_by
    )}
    assert verify(rec, log).ok, "the OK citation must verify"


def test_ok_then_failed_keeps_the_ok_call():
    result = _result(
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9a", "optimise_route", _route_out()),
        ("s9b", "optimise_route", _route_out(ToolStatus.FAILED, "stale grid")),
    )
    route = _build_route(result)
    assert route is not None
    assert route.computed_by == result.call_id_for("s9a")


# ==========================================================================
# Review floor: stateless round inference
# ==========================================================================


def test_round_1_requests_shelter_and_grid_but_no_route():
    """Verdict bad, nothing ran yet: shelter (critical) + grid, no leg."""
    result = _result(
        ("s1", "resolve_place", _place()),
        ("s6", "compute_risk_score", _risk("no_go")),
    )
    requests = agents.agent_for("risk").review(result, _intent())
    by_tool = {r.tool: r for r in requests}
    assert set(by_tool) == {"nearest_landing_centre", "route_grid"}
    assert by_tool["nearest_landing_centre"].critical
    assert by_tool["nearest_landing_centre"].to_agent == "GeospatialAgent"
    assert not by_tool["route_grid"].critical
    assert by_tool["route_grid"].args["vessel_class"] == "frp_9m"
    assert by_tool["route_grid"].args["resolution_deg"] == 0.05
    assert tuple(by_tool["route_grid"].args["bbox"]) == tuple(config.bbox())


def test_round_2_requests_the_leg_with_literals():
    """Shelter + grid OK and no route yet: exactly one optimise_route request."""
    result = _result(
        ("s1", "resolve_place", _place()),
        ("s6", "compute_risk_score", _risk("marginal")),
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
    )
    requests = agents.agent_for("risk").review(result, _intent())
    assert [r.tool for r in requests] == ["optimise_route"]
    leg = requests[0]
    assert leg.args["start"] == {"lat": 10.77, "lon": 79.84}
    assert leg.args["end"] == {"lat": 10.29, "lon": 79.85}
    assert leg.args["grid_ref"] == "grid_abc123"
    # Binding precedent: never None -- (intent.vessel_class or FRP_9M).value.
    assert leg.args["vessel_class"] == "frp_9m"


def test_failed_grid_means_no_route_request():
    """A grid that failed to build cannot be routed over; ask nothing more."""
    failed_grid = _grid()
    failed_grid = failed_grid.model_copy(
        update={"status": ToolStatus.FAILED, "error": "no bathymetry cache"}
    )
    result = _result(
        ("s1", "resolve_place", _place()),
        ("s6", "compute_risk_score", _risk("no_go")),
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", failed_grid),
    )
    requests = agents.agent_for("risk").review(result, _intent())
    assert [r.tool for r in requests] == []
    assert _build_route(result) is None


def test_round_2_does_not_repeat_once_the_leg_ran():
    """The loop must terminate: a ran leg implies no further requests."""
    result = _result(
        ("s1", "resolve_place", _place()),
        ("s6", "compute_risk_score", _risk("no_go")),
        ("s7", "nearest_landing_centre", _shelter()),
        ("s8", "route_grid", _grid()),
        ("s9", "optimise_route", _route_out()),
    )
    assert agents.agent_for("risk").review(result, _intent()) == []
