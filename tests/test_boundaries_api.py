"""GET /geo/boundaries serves the drawn line from the checked line's source.

The audit found the map drawing an empty box for geofence answers: the
tool output carries no coordinates, so there was nothing honest to draw.
These tests pin the contract that fixes it -- same treaty positions the
geofence tool tests, inside the study box, with provenance attached.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from core import config
from ingest.static.boundaries import IMBL_POSITIONS
from orchestrator.main import app


def test_boundaries_serve_the_digitised_positions() -> None:
    body = TestClient(app).get("/geo/boundaries").json()
    assert len(body["imbl"]) == len(IMBL_POSITIONS)
    first = IMBL_POSITIONS[0].as_tuple
    assert body["imbl"][0] == [round(first[0], 5), round(first[1], 5)]


def test_boundary_crosses_the_study_box() -> None:
    # The treaty line starts south of Kanyakumari, outside the box -- only
    # its crossing matters to the map, so assert intersection, not
    # containment.
    from shapely.geometry import LineString
    from shapely.geometry import box as shapely_box

    body = TestClient(app).get("/geo/boundaries").json()
    west, south, east, north = config.bbox()
    line = LineString(body["imbl"])
    assert line.intersects(shapely_box(west, south, east, north))


def test_provenance_travels_with_the_geometry() -> None:
    body = TestClient(app).get("/geo/boundaries").json()
    assert "1974" in body["source"] and "1976" in body["source"]
    assert isinstance(body["notes"], list) and len(body["notes"]) > 0
