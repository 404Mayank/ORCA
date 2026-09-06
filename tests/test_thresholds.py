"""Tests for config/risk_thresholds.yaml.

The file is a deliverable, so these tests protect its two properties that
actually matter: that it stays in sync with the code, and that every number in
it is honestly attributed.

The provenance test is the important one. It is what stops someone adding a
number at 2am with no tag, which is how an unsourced figure ends up in front
of a judge wearing an INCOIS label it never earned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.schemas import Comparison, Threshold, Unit, VesselClass

CONFIG = Path(__file__).resolve().parents[1] / "config" / "risk_thresholds.yaml"

#: The only legal provenance tags. See the header of the YAML file.
VALID_PROVENANCE = {"published", "orca", "provisional"}


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_file_parses(cfg):
    assert cfg["meta"]["version"]


def test_vessel_class_keys_match_the_enum_exactly(cfg):
    """A drift here is a KeyError at risk-scoring time. Catch it at rest.

    Both directions are checked: a class in the enum with no thresholds
    cannot be scored, and a class in the config with no enum member is dead
    configuration that will mislead whoever reads it next.
    """
    in_config = set(cfg["vessel_classes"])
    in_code = {v.value for v in VesselClass}
    assert in_config == in_code, (
        f"config-only: {sorted(in_config - in_code)}, "
        f"code-only: {sorted(in_code - in_config)}"
    )


def _walk_provenanced(node, path=""):
    """Yield (path, dict) for every mapping that carries a `provenance` key."""
    if isinstance(node, dict):
        if "provenance" in node:
            yield path, node
        for k, v in node.items():
            yield from _walk_provenanced(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_provenanced(v, f"{path}[{i}]")


def test_every_provenance_tag_is_valid(cfg):
    bad = {
        path: node["provenance"]
        for path, node in _walk_provenanced(cfg)
        if node["provenance"] not in VALID_PROVENANCE
    }
    assert not bad, f"Invalid provenance tags: {bad}"


def test_every_vessel_limit_carries_a_provenance_tag(cfg):
    """No untagged numbers. This is the rule the file exists to enforce."""
    missing: list[str] = []
    for cls, spec in cfg["vessel_classes"].items():
        for name, limit in spec["limits"].items():
            if "provenance" not in limit:
                missing.append(f"{cls}.{name}")
    assert not missing, f"Thresholds with no provenance tag: {missing}"


def test_orca_authored_limits_state_their_reasoning(cfg):
    """A number we chose ourselves must say why. 'published' numbers need not --
    the citation is the reasoning."""
    missing: list[str] = []
    for cls, spec in cfg["vessel_classes"].items():
        for name, limit in spec["limits"].items():
            if limit.get("provenance") == "orca" and not limit.get("rationale"):
                missing.append(f"{cls}.{name}")
    assert not missing, f"ORCA-set limits with no stated rationale: {missing}"


def test_we_do_not_claim_to_implement_bsi(cfg):
    """We could not obtain the BSI formula, so we must not claim it.

    Pinned as a test because this is exactly the claim that would be tempting
    to quietly upgrade during a demo rehearsal.
    """
    assert cfg["incois_small_vessel_advisory"]["index"]["implemented_by_orca"] is False
    assert cfg["derived_metrics"]["directional_spread"]["status"] == "not_implemented"


def test_published_incois_criteria_are_unchanged(cfg):
    """These are not ours to adjust.

    Verified 2026-09-04 against incois.gov.in: significant wave height 3.0-3.5 m
    puts coastal regions on alert, above 3.5 m triggers a warning; the swell
    bands are 2.5-3.0 m and above 3.0 m.
    """
    hwa = cfg["incois_high_wave_alert"]
    assert hwa["provenance"] == "published"
    assert hwa["significant_wave_height"]["alert"]["min_m"] == 3.0
    assert hwa["significant_wave_height"]["alert"]["max_m"] == 3.5
    assert hwa["significant_wave_height"]["warning"]["min_m"] == 3.5
    assert hwa["swell_height"]["alert"]["min_m"] == 2.5
    assert hwa["swell_height"]["warning"]["min_m"] == 3.0
    # Every vessel we model must sit inside the SVAS scope, or citing it is
    # misleading.
    assert cfg["incois_small_vessel_advisory"]["scope"]["max_beam_width_m"] == 7.0


def test_our_small_vessel_limits_are_stricter_than_the_area_alert(cfg):
    """The design claim, asserted rather than merely written in a comment.

    An area alert for a district is not an operating limit for one boat. If a
    later edit relaxed a small-vessel limit above the INCOIS alert floor, the
    system would be telling an open boat to launch into conditions that put a
    whole coastline on alert.
    """
    alert_floor = cfg["incois_high_wave_alert"]["significant_wave_height"]["alert"]["min_m"]
    for cls in ("kattumaram", "frp_9m"):
        limit = cfg["vessel_classes"][cls]["limits"]["significant_wave_height"]["value"]
        assert limit < alert_floor, (
            f"{cls} wave limit {limit} m is not below the INCOIS alert floor "
            f"{alert_floor} m."
        )


def test_limits_are_ordered_by_vessel_capability(cfg):
    """A bigger, decked vessel must not have a lower limit than an open boat."""
    wave = {
        cls: spec["limits"]["significant_wave_height"]["value"]
        for cls, spec in cfg["vessel_classes"].items()
    }
    assert wave["kattumaram"] < wave["frp_9m"] < wave["mechanised_trawler"]

    speed = {c: s["cruise_speed_kn"] for c, s in cfg["vessel_classes"].items()}
    assert speed["kattumaram"] < speed["frp_9m"] < speed["mechanised_trawler"]


def test_limits_load_into_real_threshold_objects(cfg):
    """The config must produce valid core.units.Threshold objects.

    This is the actual integration point: a unit string in the YAML that the
    Unit enum does not recognise is a runtime failure at risk-scoring time,
    and it should fail here instead.
    """
    built = 0
    for cls, spec in cfg["vessel_classes"].items():
        for name, limit in spec["limits"].items():
            t = Threshold(
                value=float(limit["value"]),
                unit=Unit(limit["unit"]),
                comparison=Comparison(limit["comparison"]),
                source=f"risk_thresholds.yaml#{name}.{cls}",
                label=f"{name} limit, {spec['label']}",
            )
            assert t.source.startswith("risk_thresholds.yaml#")
            built += 1
    assert built >= 6


def test_threshold_source_pointers_in_fixtures_resolve_to_real_config_entries(cfg):
    """The citation pointers used in the ideal answer must not be fiction.

    core.units.Threshold.source is a string, so nothing stops it naming a
    config path that does not exist. This walks the fixture's pointers back
    into the YAML and proves each one lands on a real value.
    """
    from fixtures.safety_assess_nagapattinam import build_ideal_safety_answer

    for driver in build_ideal_safety_answer().drivers:
        source = driver.evaluation.threshold.source
        file_part, _, path = source.partition("#")
        assert file_part == "risk_thresholds.yaml", source
        metric, vessel = path.split(".")
        entry = cfg["vessel_classes"][vessel]["limits"][metric]
        assert entry["value"] == driver.evaluation.threshold.value, (
            f"Fixture cites {source} as {driver.evaluation.threshold.value} "
            f"{driver.evaluation.threshold.unit.value} but the config says "
            f"{entry['value']} {entry['unit']}."
        )
        assert entry["unit"] == driver.evaluation.threshold.unit.value


def test_marginal_band_matches_the_code_default(cfg):
    from core.units import DEFAULT_MARGINAL_BAND

    assert cfg["verdict"]["marginal_band_fraction"]["value"] == DEFAULT_MARGINAL_BAND


def test_score_bands_are_contiguous_and_cover_zero_to_one(cfg):
    b = cfg["verdict"]["score_bands"]
    assert b["go"]["max"] == b["marginal"]["min"]
    assert b["marginal"]["max"] == b["no_go"]["min"]
    assert 0.0 < b["go"]["max"] < b["no_go"]["min"] < 1.0
