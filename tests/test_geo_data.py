"""Geo-data honesty: per-point provenance, the dataset registry, and the gaps.

Stream 3 adds no unverified coordinate and no hand-drawn polygon. What it
adds is the plumbing that makes verified data land cleanly (per-point
source/confidence, the INCOIS loader, the dataset registry) plus tests that
the unavailable geometries stay reported as unavailable.
"""

from __future__ import annotations

from core.schemas.tool_io import ToolStatus
from ingest.catalogue import resolve_datasets, source_status
from ingest.static.landing_centres import load_table
from tools.geo.geofence import IMPLEMENTED_ZONE_TYPES, UNAVAILABLE_ZONE_TYPES
from tools.geo.nearest import ResolvePlaceIn, resolve_place


def test_seed_points_carry_their_own_provenance():
    out = resolve_place(ResolvePlaceIn(name="Nagapattinam"))
    assert out.provenance.source == "bbox-seed"
    assert 0.0 < out.match_confidence < 1.0


def test_provisional_authority_is_preserved():
    out = resolve_place(ResolvePlaceIn(name="Cuddalore"))
    assert "provisional" in out.provenance.authority.lower()


def test_registry_lists_every_live_layer_once():
    records = resolve_datasets()
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate dataset ids"
    for needed in (
        "open_meteo_marine", "jplMURSST41", "gdacs_tc", "srtm30plus",
        "imbl_treaties", "incois_landing_centres", "gom_mpa", "eez_india_east",
    ):
        assert needed in ids


def test_wanted_sources_are_flagged_not_hidden():
    assert source_status("incois_landing_centres") == "wanted"
    assert source_status("gom_mpa") == "wanted"
    assert source_status("eez_india_east") == "wanted"
    assert source_status("gdacs_tc") == "live"
    assert source_status("no_such_source") is None


def test_landing_table_is_empty_until_ingested():
    # The INCOIS set is not obtained; the loader must say so with {} rather
    # than with seed data wearing an official source string.
    assert load_table() == {}


def test_geofence_still_reports_what_it_cannot_check():
    assert IMPLEMENTED_ZONE_TYPES == {"imbl"}
    for missing in ("mpa", "eez"):
        assert missing in UNAVAILABLE_ZONE_TYPES


def test_verified_landing_centres_resolve_in_box():
    from core import config

    points = config.load_yaml("bbox.yaml")["reference_points"]
    assert len(points) == 20
    for name in (
        "pamban", "mandapam", "thondi", "devipattinam", "karaikal",
        "tharangambadi", "pazhayar", "parangipettai", "mudasalodai",
        "adirampattinam", "muthupet", "kottaipattinam", "sethubhavachatram",
    ):
        out = resolve_place(ResolvePlaceIn(name=name.title()))
        assert out.status is not ToolStatus.FAILED, name
        assert out.in_bbox, name
        assert out.match_confidence in (0.85, 0.9), name


def test_single_source_points_carry_lower_confidence():
    for name, expected in (
        ("Mudasalodai", 0.85),
        ("Pazhayar", 0.85),
        ("Sethubhavachatram", 0.85),
        ("Pamban", 0.9),
    ):
        out = resolve_place(ResolvePlaceIn(name=name))
        assert out.match_confidence == expected, name
