"""Typed access to the YAML files in ``config/``.

Config is loaded once and cached. Every consumer goes through here rather than
calling ``yaml.safe_load`` itself, so that a missing key fails in one place with
a message that says which file and which key, instead of surfacing as a
``KeyError: 'frp_9m'`` from inside the risk function.

``config/risk_thresholds.yaml`` is a deliverable, not a settings file. This
module deliberately does not paper over anything missing from it: a vessel
class with no thresholds raises, because scoring a boat we have no limits for
is worse than refusing to score it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

import core.env  # noqa: F401 -- loads .env before any config is read

from core.units import Comparison, Threshold, Unit

__all__ = [
    "CONFIG_DIR",
    "load_yaml",
    "bbox",
    "grid",
    "risk_thresholds",
    "vessel_spec",
    "thresholds_for",
    "score_bands",
    "marginal_band",
    "in_bbox",
]

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# bbox.yaml
# --------------------------------------------------------------------------


def bbox() -> tuple[float, float, float, float]:
    """(west, south, east, north) in EPSG:4326."""
    b = load_yaml("bbox.yaml")["bbox"]
    return (b["lon_min"], b["lat_min"], b["lon_max"], b["lat_max"])


def grid() -> dict[str, Any]:
    return load_yaml("bbox.yaml")["grid"]


def in_bbox(lat: float, lon: float) -> bool:
    """Whether a point is inside the South Coromandel box.

    Drives the ``out_of_region`` refusal. Inclusive of the boundary, since a
    landing centre sitting exactly on a rounded bound should be served rather
    than refused.
    """
    west, south, east, north = bbox()
    return south <= lat <= north and west <= lon <= east


# --------------------------------------------------------------------------
# risk_thresholds.yaml
# --------------------------------------------------------------------------


def risk_thresholds() -> dict[str, Any]:
    return load_yaml("risk_thresholds.yaml")


def vessel_spec(vessel_class: str) -> dict[str, Any]:
    classes = risk_thresholds()["vessel_classes"]
    if vessel_class not in classes:
        raise KeyError(
            f"No thresholds configured for vessel class {vessel_class!r}. "
            f"Configured: {sorted(classes)}. Scoring a vessel we have no limits "
            "for is worse than refusing to score it."
        )
    return classes[vessel_class]


@lru_cache(maxsize=None)
def thresholds_for(vessel_class: str) -> dict[str, Threshold]:
    """Build real :class:`~core.units.Threshold` objects for a vessel class.

    The ``source`` pointer is generated here in the same
    ``risk_thresholds.yaml#<metric>.<class>`` form the fixtures use, so a
    citation shown in the UI can always be walked back to the config entry it
    came from. ``tests/test_thresholds.py`` asserts that round trip.
    """
    spec = vessel_spec(vessel_class)
    out: dict[str, Threshold] = {}
    for metric, limit in spec["limits"].items():
        out[metric] = Threshold(
            value=float(limit["value"]),
            unit=Unit(limit["unit"]),
            comparison=Comparison(limit["comparison"]),
            source=f"risk_thresholds.yaml#{metric}.{vessel_class}",
            label=f"{metric.replace('_', ' ')} limit, {spec['label']}",
        )
    return out


def score_bands() -> dict[str, dict[str, float]]:
    return risk_thresholds()["verdict"]["score_bands"]


def marginal_band() -> float:
    return float(risk_thresholds()["verdict"]["marginal_band_fraction"]["value"])


def cruise_speed_kn(vessel_class: str) -> float:
    return float(vessel_spec(vessel_class)["cruise_speed_kn"])
