"""Tests for the geospatial tools and the digitised maritime boundary.

The geofence tests carry more weight than most of the suite. Crossing the IMBL
is what gets a Tamil Nadu fisherman arrested by the Sri Lankan Navy, so a wrong
answer here has a consequence no amount of good UI makes up for.
"""

from __future__ import annotations

import pytest
from shapely.geometry import Point

from core.schemas.tool_io import GeoPoint
from ingest.static.boundaries import (
    BAY_OF_BENGAL_1976,
    GULF_OF_MANNAR_1976,
    IMBL_POSITIONS,
    PALK_BAY_1974,
    dms_to_dd,
    imbl_linestring,
    indian_waters_polygon,
)
from tools.geo.geofence import GeofenceCheckIn, geofence_check
from tools.geo.nearest import (
    DistanceBearingIn,
    NearestLandingCentreIn,
    ResolvePlaceIn,
    distance_bearing,
    nearest_landing_centre,
    resolve_place,
)


def _pt(lat: float, lon: float) -> GeoPoint:
    return GeoPoint(lat=lat, lon=lon)


def _check(lat, lon, buffer_km=0.0, zone_types=None):
    return geofence_check(
        GeofenceCheckIn(
            points=[_pt(lat, lon)],
            buffer_km=buffer_km,
            zone_types=zone_types or ["imbl"],
        )
    )


# ==========================================================================
# The treaty transcription
# ==========================================================================


def test_treaty_position_counts_match_the_agreements():
    """1974 lists six positions; 1976 lists thirteen and eight.

    The thirteen is a useful independent check -- CLAUDE.md, written before
    the treaty was fetched, also says the Gulf of Mannar has 13 turning points.
    """
    assert len(PALK_BAY_1974) == 6
    assert len(GULF_OF_MANNAR_1976) == 13
    assert len(BAY_OF_BENGAL_1976) == 8


def test_the_three_treaty_segments_meet_exactly():
    """The strongest automatic check available on a hand-typed coordinate table.

    The 1974 line's southern end must be the 1976 Gulf of Mannar line's start,
    and its northern end the Bay of Bengal line's start. A single mistyped
    digit in either junction breaks this.
    """
    assert (PALK_BAY_1974[-1].lat, PALK_BAY_1974[-1].lon) == (
        GULF_OF_MANNAR_1976[0].lat,
        GULF_OF_MANNAR_1976[0].lon,
    )
    assert (PALK_BAY_1974[0].lat, PALK_BAY_1974[0].lon) == (
        BAY_OF_BENGAL_1976[0].lat,
        BAY_OF_BENGAL_1976[0].lon,
    )


def test_continuous_boundary_drops_the_duplicated_junctions():
    """6 + 13 + 8 = 27 positions, minus the two shared junctions = 25."""
    assert len(IMBL_POSITIONS) == 25


def test_dms_conversion():
    assert dms_to_dd(9, 40.15) == pytest.approx(9.669167, abs=1e-6)
    assert dms_to_dd(79, 32.0) == pytest.approx(79.533333, abs=1e-6)


def test_boundary_runs_south_west_to_north_east():
    """Ordering matters: the LineString must not double back on itself."""
    lats = [p.lat for p in IMBL_POSITIONS]
    assert lats[0] < lats[-1]
    assert imbl_linestring().is_valid
    assert imbl_linestring().is_simple, "the boundary must not self-intersect"


def test_all_positions_are_plausible_for_this_region():
    """Catches a transposed lat/lon or a dropped degree, which would otherwise
    put the boundary in the wrong hemisphere and still 'work'."""
    for p in IMBL_POSITIONS:
        assert 4.0 <= p.lat <= 12.0, f"{p.label} latitude {p.lat} is off the map"
        assert 77.0 <= p.lon <= 84.0, f"{p.label} longitude {p.lon} is off the map"


# ==========================================================================
# Which side of the line
# ==========================================================================


def test_kachchatheevu_is_on_the_sri_lankan_side():
    """The islet the 1974 agreement placed under Sri Lankan sovereignty.

    The single best correctness check available: it is the most politically
    consequential point on this boundary, and if the geometry put it on the
    Indian side the whole digitisation would be wrong.
    """
    assert not indian_waters_polygon().contains(Point(79.517, 9.383))


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("Nagapattinam", 10.77, 79.84),
        ("Rameswaram", 9.29, 79.31),
        ("off Cuddalore", 11.75, 79.90),
        ("Tuticorin approach", 8.80, 78.60),
    ],
)
def test_indian_ports_and_their_grounds_are_indian_waters(name, lat, lon):
    assert indian_waters_polygon().contains(Point(lon, lat)), name


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("north of Jaffna", 9.90, 80.30),
        ("mid Gulf of Mannar", 8.60, 79.20),
        ("Kachchatheevu", 9.383, 79.517),
    ],
)
def test_sri_lankan_side_points_are_not_indian_waters(name, lat, lon):
    assert not indian_waters_polygon().contains(Point(lon, lat)), name


def test_a_fixed_compass_rule_would_not_work():
    """Why the polygon split exists rather than an 'east of the line' test.

    The boundary turns sharply at Adam's Bridge. In the Gulf of Mannar, Sri
    Lanka is EAST of the line; in the Bay of Bengal it is SOUTH of it. Any
    single compass rule gets one of these wrong, which is why side is decided
    by containment instead.
    """
    indian = indian_waters_polygon()
    # Same longitude, opposite verdicts -- so longitude alone cannot decide.
    assert indian.contains(Point(79.84, 10.77))  # Nagapattinam, Indian
    assert not indian.contains(Point(79.84, 8.60))  # Gulf of Mannar, Sri Lankan


# ==========================================================================
# geofence_check
# ==========================================================================


def test_a_point_in_indian_waters_is_clear():
    result = _check(10.77, 79.84)
    assert result.clear
    assert result.hits == []


def test_a_point_across_the_boundary_is_flagged_as_inside():
    result = _check(9.383, 79.517)
    assert not result.clear
    hit = result.hits[0]
    assert hit.zone_type == "imbl"
    assert hit.relation == "inside"
    assert hit.distance_km < 0, "negative distance signals 'already across'"
    assert "1974" in hit.authority


def test_rameswaram_is_within_a_25km_buffer_of_the_boundary():
    """Not a violation, but the warning that matters most to those fishermen.

    Rameswaram sits about 23 km from the line, which is why boats working out
    of it are the ones routinely detained.
    """
    result = _check(9.29, 79.31, buffer_km=25.0)
    assert not result.clear
    hit = result.hits[0]
    assert hit.relation == "within_buffer"
    assert 20.0 < hit.distance_km < 26.0


def test_no_buffer_means_no_proximity_warning():
    assert _check(9.29, 79.31, buffer_km=0.0).clear


def test_a_track_crossing_the_line_is_caught_even_when_endpoints_are_not_tested():
    """A path is more than its vertices.

    Two waypoints can both sit on the Indian side while the leg between them
    cuts a corner across the boundary. Checking only endpoints would clear
    exactly the track that gets a boat detained.
    """
    result = geofence_check(
        GeofenceCheckIn(
            points=[_pt(9.29, 79.31), _pt(9.30, 79.80)], zone_types=["imbl"]
        )
    )
    assert not result.clear
    assert any(h.relation == "crosses" for h in result.hits)


def test_unavailable_zone_types_degrade_rather_than_silently_shrink():
    """`clear` must mean clear of what was actually checked, and say what that was.

    The MPA and EEZ geometries are not obtained yet. A request for them must
    not quietly reduce to an IMBL-only check reported as a clean pass.
    """
    result = geofence_check(
        GeofenceCheckIn(
            points=[_pt(10.77, 79.84)], zone_types=["imbl", "mpa", "restricted"]
        )
    )
    assert result.status.value == "degraded"
    assert result.zones_checked == ["imbl"]
    assert any("mpa" in n.lower() or "MPA" in n for n in result.notes)
    assert result.error is not None


def test_geometry_caveats_travel_with_every_answer():
    """The great-circle approximation and the advisory-only warning are stated
    in the response, not buried in a docstring."""
    notes = " ".join(_check(10.77, 79.84).notes)
    assert "GREAT CIRCLES" in notes
    assert "Not for navigation" in notes


def test_provenance_points_at_the_treaty():
    result = _check(10.77, 79.84)
    assert result.provenance.source == "treaty_digitised"
    assert "un.org" in result.provenance.source_url


# ==========================================================================
# resolve_place / distance / nearest
# ==========================================================================


def test_resolve_place_returns_a_coordinate_and_bbox_membership():
    out = resolve_place(ResolvePlaceIn(name="Nagapattinam"))
    assert (out.lat, out.lon) == (10.77, 79.84)
    assert out.in_bbox


def test_resolve_place_flags_a_point_outside_the_study_box():
    """Kochi is on the west coast. This is what drives an out_of_region refusal."""
    out = resolve_place(ResolvePlaceIn(name="Kochi"))
    assert not out.in_bbox


def test_resolve_place_fails_loudly_on_an_unknown_name():
    """Better a failed call than a confidently wrong coordinate."""
    out = resolve_place(ResolvePlaceIn(name="Atlantis"))
    assert out.status.value == "failed"
    assert out.match_confidence == 0.0
    assert "gazetteer" in out.error


def test_resolve_place_never_claims_full_confidence():
    """The gazetteer is a provisional seed from bbox.yaml, not the INCOIS
    landing centre register. Nothing downstream may mistake one for the other."""
    out = resolve_place(ResolvePlaceIn(name="Nagapattinam"))
    assert out.match_confidence < 1.0
    assert out.provenance.source == "bbox.yaml#reference_points"
    assert "provisional" in out.provenance.authority.lower()


def test_distance_bearing_is_geodesic():
    out = distance_bearing(
        DistanceBearingIn(from_lat=10.77, from_lon=79.84, to_lat=9.29, to_lon=79.31)
    )
    assert out.method == "geodesic_wgs84"
    assert 170.0 < out.distance_km < 178.0
    assert 190.0 < out.bearing_deg < 210.0  # roughly south-southwest


def test_distance_to_self_is_zero():
    out = distance_bearing(
        DistanceBearingIn(from_lat=10.77, from_lon=79.84, to_lat=10.77, to_lon=79.84)
    )
    assert out.distance_km == 0.0


def test_nearest_landing_centre_finds_the_obvious_one():
    out = nearest_landing_centre(NearestLandingCentreIn(lat=10.80, lon=79.90))
    assert out.name == "Nagapattinam"
    assert out.distance_km < 15.0
    assert 0.0 <= out.bearing_deg < 360.0
