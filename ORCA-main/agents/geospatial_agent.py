"""Geospatial domain: place resolution, distance, geofences, landing centres.

**This agent owns the coordinate.** Every other domain's steps depend on the
place it resolves, which is why its steps are always planned first and always
start at ``s1``. One resolver means one provenance trail for the position, and
a position is the thing the whole answer hangs off -- a safety verdict computed
25 km from where the fisherman actually is would be worse than no verdict.

The governing rule bites hard here. From ``tools/geo/nearest.py``: *the LLM may
say the word "Nagapattinam"; it may never produce the coordinate.* The planner
passes a name through as a string. This agent turns it into numbers, and the
numbers come from the gazetteer.

No LLM here. See ``agents/base.py``.
"""

from __future__ import annotations

from typing import Any

from core.schemas.intent import Intent, QueryType
from core.schemas.tool_io import ToolStatus
from agents.base import Agent, DomainFragment, register_agent
from tools.registry import AgentGroup


class GeospatialAgent(Agent):
    group = AgentGroup.GEOSPATIAL
    label = "Geospatial"

    #: Zone types checked by default. IMBL is the only one with real geometry
    #: today -- MPA and EEZ are not obtained -- and geofence_check reports what
    #: it actually checked in ``zones_checked`` rather than implying coverage
    #: it does not have.
    DEFAULT_ZONES = ("imbl",)

    def plan_steps(self, intent: Intent, base_id: int = 1) -> list[dict[str, Any]]:
        place = intent.spatial_reference.name if intent.spatial_reference else None
        if not place:
            # No place, no plan. The planner's safety gate should already have
            # asked for one; returning nothing here means a plan composed from
            # agents is empty rather than subtly wrong, which is the failure
            # mode we want.
            return []

        steps: list[dict[str, Any]] = [
            {
                "id": f"s{base_id}",
                "tool": "resolve_place",
                "args": {"name": "$PLACE"},
                "agent": self.group,
            }
        ]
        origin = {"lat": f"$s{base_id}.lat", "lon": f"$s{base_id}.lon"}

        if intent.query_type in (QueryType.GEOFENCE_CHECK, QueryType.PFZ_LOCATE):
            steps.append(
                {
                    "id": f"s{base_id + 1}",
                    "tool": "geofence_check",
                    "args": {
                        "points": [origin],
                        **({"buffer_km": 5.0} if intent.query_type is QueryType.GEOFENCE_CHECK else {}),
                    },
                    "depends_on": [f"s{base_id}"],
                    "agent": self.group,
                }
            )
        return steps

    def fragment(self, result, intent: Intent) -> DomainFragment:
        mine, failed, status = self._collect(result, intent)
        findings = []
        notes: list[str] = []
        records = self._my_records(result)

        for step_id in mine:
            record = records.get(step_id)
            output = result.outputs.get(step_id)
            if record is None or output is None:
                continue
            tool = record.tool

            if output.status is ToolStatus.FAILED:
                findings.append(
                    self._finding(
                        result, step_id, f"{tool}_failed", "geo.tool_failed",
                        {"tool": tool, "error": output.error or "no detail"},
                        # A failed place resolution is safety-critical by
                        # consequence: everything downstream would be computed
                        # for an unknown position.
                        safety_critical=(tool == "resolve_place"),
                    )
                )
                continue

            if tool == "resolve_place":
                findings.append(
                    self._finding(
                        result, step_id, "place", "geo.place",
                        {
                            "name": output.matched_name,
                            "lat": output.lat,
                            "lon": output.lon,
                            "in_bbox": output.in_bbox,
                            "confidence": output.match_confidence,
                        },
                    )
                )
                if not output.in_bbox:
                    notes.append(
                        f"{output.matched_name} is outside the South Coromandel box; "
                        "the answer must be a refusal, not a forecast"
                    )
                if output.match_confidence < 1.0:
                    notes.append(
                        "gazetteer is the provisional 7-point seed from bbox.yaml; "
                        "full confidence needs the INCOIS landing centre set"
                    )
            elif tool == "geofence_check":
                findings.append(
                    self._finding(
                        result, step_id, "geofence", "geo.geofence",
                        {
                            "clear": output.clear,
                            "hit_count": len(output.hits),
                            "zones_checked": output.zones_checked,
                        },
                        # Crossing the IMBL is an arrest, not a discomfort.
                        safety_critical=True,
                    )
                )
                missing = [z for z in ("mpa", "eez") if z not in output.zones_checked]
                if missing:
                    notes.append(
                        f"no geometry for {', '.join(missing)}; "
                        "'clear' means clear of what was checked, not of everything"
                    )
            elif tool == "nearest_landing_centre":
                findings.append(
                    self._finding(
                        result, step_id, "nearest_landing", "geo.landing_centre",
                        {
                            "name": output.name,
                            "distance_km": output.distance_km,
                            "bearing_deg": output.bearing_deg,
                            "district": output.district,
                        },
                    )
                )
            elif tool == "distance_bearing":
                findings.append(
                    self._finding(
                        result, step_id, "distance", "geo.distance",
                        {
                            "distance_km": output.distance_km,
                            "bearing_deg": output.bearing_deg,
                            "method": output.method,
                        },
                    )
                )

        return DomainFragment(
            agent=self.name,
            status=status,
            findings=findings,
            step_ids=mine,
            failed_steps=failed,
            notes=notes,
        )


geospatial_agent = register_agent(GeospatialAgent())
