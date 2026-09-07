"""Weather domain: waves, wind, tides, and the alert check.

Owns four tools and one judgement that belongs to no single tool: **which of
its failures are allowed to be quiet.**

A failed tide read is a nuisance -- the answer loses a "be back before the ebb"
line and is otherwise intact. A failed alert check is not a nuisance. Not
knowing whether a cyclone is active is materially different from knowing there
is none, and it must reach the verdict. ``active_alerts`` is registered
``safety_critical=True`` and this agent is where that flag becomes a finding
the synthesis layer cannot overlook.

No LLM here. See ``agents/base.py`` for why that is deliberate rather than
unfinished.
"""

from __future__ import annotations

from typing import Any

from core.schemas.intent import Intent, QueryType
from core.schemas.tool_io import ToolStatus
from agents.base import Agent, DomainFragment, register_agent
from tools.registry import AgentGroup


class WeatherAgent(Agent):
    group = AgentGroup.WEATHER
    label = "Weather"

    #: Intents that need conditions at all. A geofence question is about
    #: boundaries, not weather, and adding a wave call to it would spend a
    #: network round trip to produce a number nothing reads.
    WANTS_CONDITIONS = {QueryType.SAFETY_ASSESS}

    def plan_steps(self, intent: Intent, base_id: int = 1) -> list[dict[str, Any]]:
        if intent.query_type not in self.WANTS_CONDITIONS:
            return []

        # Every step depends on s1 (resolve_place) by convention: the
        # geospatial agent always plans first and owns the coordinate. Weather
        # never resolves a place itself -- one resolver, one provenance trail.
        origin = {"lat": "$s1.lat", "lon": "$s1.lon"}
        steps = [
            {
                "id": f"s{base_id}",
                "tool": "wave_forecast",
                "args": {**origin, "hours": 24},
                "depends_on": ["s1"],
                "agent": self.group,
            },
            {
                "id": f"s{base_id + 1}",
                "tool": "wind_forecast",
                "args": {**origin, "hours": 24},
                "depends_on": ["s1"],
                "agent": self.group,
            },
            {
                "id": f"s{base_id + 2}",
                "tool": "tides",
                "args": {**origin, "hours": 24},
                "depends_on": ["s1"],
                # Tide is context, not a limit, for a day-fishing decision on
                # this coast. Marked optional so its failure degrades quietly.
                "optional": True,
                "agent": self.group,
            },
            {
                "id": f"s{base_id + 3}",
                "tool": "active_alerts",
                "args": {**origin},
                "depends_on": ["s1"],
                # NEVER optional. validate_plan() rejects a safety-critical
                # step marked optional, so this is enforced, not merely
                # intended.
                "optional": False,
                "agent": self.group,
            },
        ]
        return steps

    def fragment(self, result, intent: Intent) -> DomainFragment:
        mine, failed, status = self._collect(result, intent)
        findings = []
        notes: list[str] = []

        for step_id in mine:
            record = self._my_records(result).get(step_id)
            output = result.outputs.get(step_id)
            if record is None or output is None:
                continue
            tool = record.tool

            if output.status is ToolStatus.FAILED:
                critical = tool == "active_alerts"
                findings.append(
                    self._finding(
                        result,
                        step_id,
                        key=f"{tool}_failed",
                        template="weather.tool_failed",
                        slots={"tool": tool, "error": output.error or "no detail"},
                        safety_critical=critical,
                    )
                )
                if critical:
                    notes.append(
                        "alert check did not complete; a clean verdict is not "
                        "available regardless of conditions"
                    )
                continue

            if tool == "wave_forecast":
                findings.append(
                    self._finding(
                        result, step_id, "wave_height", "weather.waves",
                        {
                            "min_m": output.significant_wave_height.min,
                            "max_m": output.significant_wave_height.max,
                            "period_s_min": output.wave_period.min,
                            "period_s_max": output.wave_period.max,
                            "steepness": output.max_steepness,
                        },
                    )
                )
            elif tool == "wind_forecast":
                findings.append(
                    self._finding(
                        result, step_id, "wind_speed", "weather.wind",
                        {
                            "min_kn": output.wind_speed.min,
                            "max_kn": output.wind_speed.max,
                            "direction_deg": output.direction_deg,
                            "visibility_min_km": (
                                output.visibility.min if output.visibility else None
                            ),
                        },
                    )
                )
            elif tool == "tides":
                findings.append(
                    self._finding(
                        result, step_id, "tide", "weather.tide",
                        {
                            "range_m_min": output.tidal_range.min,
                            "range_m_max": output.tidal_range.max,
                            "is_limiting": output.is_limiting,
                        },
                    )
                )
            elif tool == "active_alerts":
                # An empty list here is a first-class negative finding, not a
                # missing field. It is only reachable when checked is True.
                findings.append(
                    self._finding(
                        result, step_id, "alerts", "weather.alerts",
                        {
                            "count": output.count,
                            "checked": output.checked,
                            "types": output.checked_types,
                        },
                        safety_critical=True,
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


weather_agent = register_agent(WeatherAgent())
