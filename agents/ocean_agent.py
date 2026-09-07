"""Ocean domain: SST fronts, chlorophyll, anomalies, PFZ candidates.

Unblocked on 2026-09-06. This agent was dead for the whole of Phase 1 because
its three tools were waiting on a Copernicus Marine account. They now run
against NOAA CoastWatch ERDDAP -- MUR L4 SST at 0.01 deg and DINEOF gap-filled
VIIRS chlorophyll, both keyless and both near-real-time. See
``ingest/sources/erddap.py`` for why those satisfy CLAUDE.md's requirement
(gap-filled L4) without satisfying its suggestion (Copernicus specifically).

The domain judgement this agent owns: **a missing ocean layer is not a safety
failure.** If chlorophyll is stale, a PFZ answer gets weaker or disappears; a
fisherman is not endangered by it. That is the opposite of the weather agent's
alert check, and keeping the two policies in their own files is the reason
these classes exist rather than one generic handler.

No LLM here. Hypotheses for ``causal_explain`` are *proposed* by the planner
LLM and *tested* here in code; an untested hypothesis is dropped, never
reported.
"""

from __future__ import annotations

from typing import Any

from core.schemas.intent import Intent, QueryType
from core.schemas.tool_io import ToolStatus
from agents.base import Agent, AgentRequest, DomainFragment, register_agent
from tools.registry import AgentGroup

#: Beyond this many standard deviations from the monthly baseline, a
#: chlorophyll reading stops being ordinary variation and becomes something
#: causal_explain should test a hypothesis against. Two sigma is the
#: conventional line and is used here as a *flag*, never as a verdict.
ANOMALY_SIGMA_NOTABLE = 2.0


class OceanAgent(Agent):
    group = AgentGroup.OCEAN
    label = "Ocean"

    def plan_steps(self, intent: Intent, base_id: int = 1) -> list[dict[str, Any]]:
        origin = {"lat": "$s1.lat", "lon": "$s1.lon"}

        if intent.query_type is QueryType.PFZ_LOCATE:
            step = {
                "id": f"s{base_id}",
                "tool": "pfz_candidates",
                "args": {**origin, "max_distance_km": 60.0},
                "depends_on": ["s1"],
                "agent": self.group,
            }
            if intent.vessel_class is not None:
                # Range-capping by vessel class is what stops the tool
                # recommending a zone 60 km out to a kattumaram that works 12.
                step["args"]["vessel_class"] = intent.vessel_class.value
            return [step]

        if intent.query_type is QueryType.CAUSAL_EXPLAIN:
            return [
                {
                    "id": f"s{base_id}",
                    "tool": "chl_anomaly",
                    "args": {**origin, "radius_km": 25.0},
                    "depends_on": ["s1"],
                    "agent": self.group,
                },
                {
                    "id": f"s{base_id + 1}",
                    "tool": "thermal_front",
                    "args": {"bbox": [78.5, 8.0, 82.0, 12.0], "min_gradient_deg_c_per_km": 0.05},
                    "depends_on": ["s1"],
                    # Front structure is corroboration for a productivity
                    # hypothesis, not the hypothesis itself.
                    "optional": True,
                    "agent": self.group,
                },
            ]

        return []

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
                        result, step_id, f"{tool}_failed", "ocean.tool_failed",
                        {"tool": tool, "error": output.error or "no detail"},
                        # Never safety-critical. A missing ocean layer costs
                        # the answer its fishing advice; it does not put anyone
                        # to sea in conditions we failed to check.
                        safety_critical=False,
                    )
                )
                continue

            if tool == "pfz_candidates":
                best = output.candidates[0] if output.candidates else None
                findings.append(
                    self._finding(
                        result, step_id, "pfz", "ocean.pfz",
                        {
                            "count": len(output.candidates),
                            "best_distance_km": best.distance_km if best else None,
                            "best_bearing_deg": best.bearing_deg if best else None,
                            "best_score": best.score if best else None,
                            "best_gradient": best.front_gradient_deg_c_per_km if best else None,
                            "best_chl": best.chl_mg_m3 if best else None,
                            "codes": best.rationale_codes if best else [],
                        },
                    )
                )
                if not output.candidates:
                    # A real answer, and the honest one on a thermally flat
                    # day or for a short-range boat. Not an error.
                    notes.append(
                        "no PFZ candidate met the front and chlorophyll criteria "
                        "within range; reported as a negative finding, not a gap"
                    )
                if output.official_advisory_agrees is None:
                    notes.append(
                        "no official INCOIS advisory wired, so corroboration is "
                        "'not compared' rather than 'does not agree'"
                    )
            elif tool == "chl_anomaly":
                sigma = output.anomaly_sigma
                findings.append(
                    self._finding(
                        result, step_id, "chl_anomaly", "ocean.chl_anomaly",
                        {
                            "min_mg_m3": output.concentration.min,
                            "max_mg_m3": output.concentration.max,
                            "climatology_mean": output.climatology_mean,
                            "climatology_std": output.climatology_std,
                            "sigma": sigma,
                            "month": output.month,
                            "notable": sigma is not None and abs(sigma) >= ANOMALY_SIGMA_NOTABLE,
                        },
                    )
                )
                if sigma is None:
                    notes.append(
                        "no monthly baseline for this pixel, so no anomaly was "
                        "computed; the concentration alone cannot support a "
                        "claim that productivity has changed"
                    )
            elif tool == "thermal_front":
                findings.append(
                    self._finding(
                        result, step_id, "fronts", "ocean.fronts",
                        {
                            "count": len(output.fronts),
                            "max_gradient": output.max_gradient_deg_c_per_km,
                            "strongest_length_km": (
                                output.fronts[0].length_km if output.fronts else None
                            ),
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


    # ----------------------------------------------------------------------
    # Collaboration
    # ----------------------------------------------------------------------

    #: Beyond this, a candidate zone is far enough offshore that conditions on
    #: the way out stop being the same as conditions at the port.
    FAR_OFFSHORE_KM = 30.0

    def review(self, result, intent: Intent) -> list[AgentRequest]:
        """Ask other domains about the zones this one found.

        **The important one closes a real safety gap.** ``pfz_candidates``
        derives fishing zones from satellite fronts and knows nothing about
        maritime boundaries. The plan geofences the *departure point*, not the
        destination -- so before this existed, ORCA could tell a fisherman to
        work a zone 40 km offshore without ever checking whether that water is
        on the Indian side of the IMBL. Crossing it means arrest and seizure of
        the vessel.

        The ocean agent cannot answer that question. The geospatial agent can.
        So it asks, and it asks only when a candidate actually exists -- which
        is why this is a runtime decision rather than a step the composer could
        have added up front.
        """
        requests: list[AgentRequest] = []
        records = self._my_records(result)

        for step_id, record in records.items():
            output = result.outputs.get(step_id)
            if output is None or record.tool != "pfz_candidates":
                continue
            if output.status is ToolStatus.FAILED or not output.candidates:
                continue

            best = output.candidates[0]
            point = {"lat": best.centroid.lat, "lon": best.centroid.lon}

            requests.append(
                AgentRequest(
                    from_agent=self.name,
                    to_agent="GeospatialAgent",
                    tool="geofence_check",
                    args={"points": [point]},
                    reason=(
                        f"candidate zone is {best.distance_km} km offshore; the plan "
                        "geofenced the departure point, not the destination"
                    ),
                    # Boundary crossing is an arrest, not a discomfort.
                    critical=True,
                )
            )

            if best.distance_km >= self.FAR_OFFSHORE_KM:
                requests.append(
                    AgentRequest(
                        from_agent=self.name,
                        to_agent="WeatherAgent",
                        tool="wave_forecast",
                        args={**point, "hours": 12},
                        reason=(
                            f"zone is {best.distance_km} km out; conditions there are "
                            "not the conditions at the port"
                        ),
                    )
                )
        return requests


ocean_agent = register_agent(OceanAgent())
