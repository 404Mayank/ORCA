"""Tool layer. Importing this package populates the registry.

Every tool module registers its spec at import time, so ``import tools`` is
what makes ``tools.registry`` complete. Anything that validates a plan must
import this package first, or it will report perfectly good tools as unknown.
"""

from __future__ import annotations

# Imported for side effects: each module calls tools.registry.register().
from tools.geo import geofence, nearest, route_grid  # noqa: F401
from tools.ocean import chl_anomaly, pfz_candidates, thermal_front  # noqa: F401
from tools.risk import risk_score, route_optimise  # noqa: F401
from tools.weather import alerts, tides, wave_forecast, wind_forecast  # noqa: F401

__all__ = ["registry"]
