"""Risk domain: the verdict, and eventually the route.

The smallest agent and the one with the most consequence. It owns exactly one
implemented tool -- ``compute_risk_score`` -- and its whole job is to wire the
other domains' outputs into it correctly.

**This agent does not decide anything.** It does not weigh a 2.8 m swell
against a vessel class; ``compute_risk_score`` does, from thresholds in
``config/risk_thresholds.yaml``, each with a citation. What this agent knows is
narrower and still worth encoding: *which* step feeds *which* argument, and --
the part that has already caused one real bug -- that the risk step must still
run when the alert check has failed.

That bug is worth restating, because it is the reason this class does not
simply skip on a failed dependency. From PROGRESS.md: the executor once skipped
the risk step when the alert check failed, and the result was **no verdict at
all**. A cautious verdict is a worse answer than a confident one and a far
better answer than silence. ``active_alerts`` returns a well-formed output with
``checked=False``, ``compute_risk_score`` reads that single bit and forces
``no_go``, and the fisherman learns to stay in rather than learning nothing.

No LLM here. See ``agents/base.py``.
"""

from __future__ import annotations

from typing import Any

from core.schemas.intent import Intent, QueryType
from core.schemas.tool_io import ToolStatus
from agents.base import Agent, AgentRequest, DomainFragment, register_agent
from tools.registry import AgentGroup


class RiskAgent(Agent):
    group = AgentGroup.RISK
    label = "Risk"

    def plan_steps(self, intent: Intent, base_id: int = 1) -> list[dict[str, Any]]:
        if intent.query_type is not QueryType.SAFETY_ASSESS:
            return []
        if intent.vessel_class is None:
            # No vessel class, no thresholds, no score. config.thresholds_for()
            # raises rather than guessing, and refusing to plan the step is the
            # same policy one layer up: scoring a boat we have no limits for is
            # worse than refusing to score it.
            return []

        # Step ids are resolved by the composer; these names match the weather
        # agent's ordering when both are composed by compose_plan().
        return [
            {
                "id": f"s{base_id}",
                "tool": "compute_risk_score",
                "args": {
                    "vessel_class": "$VESSEL_CLASS",
                    "wave_height": "$sWAVE.significant_wave_height",
                    "wind_speed": "$sWIND.wind_speed",
                    "visibility": "$sWIND.visibility",
                    "wave_steepness": "$sWAVE.max_steepness",
                    "alerts_active": "$sALERT.count",
                    "alerts_checked": "$sALERT.checked",
                },
                # Depends on the weather steps, but NOT in a way that lets a
                # failed alert check skip it. The executor only skips a step
                # whose dependency produced no object at all; a structured
                # failure still carries checked=False, which is exactly what
                # this tool is built to consume.
                "depends_on": ["$sWAVE", "$sWIND", "$sALERT"],
                "optional": False,
                "agent": self.group,
            }
        ]

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

            if output.status is ToolStatus.FAILED:
                findings.append(
                    self._finding(
                        result, step_id, f"{record.tool}_failed", "risk.tool_failed",
                        {"tool": record.tool, "error": output.error or "no detail"},
                        # A failed risk computation is the one failure that
                        # leaves a safety question with nothing to say.
                        safety_critical=(record.tool == "compute_risk_score"),
                    )
                )
                continue

            if record.tool == "compute_risk_score":
                findings.append(
                    self._finding(
                        result, step_id, "verdict", "risk.verdict",
                        {
                            "verdict": output.verdict,
                            "score": output.score,
                            "band": output.band,
                            "limiting_driver": output.limiting_driver,
                            "downgraded": output.downgraded,
                            "downgrade_reason": output.downgrade_reason,
                            "drivers": [
                                {
                                    "id": c.driver_id,
                                    "contribution": c.contribution,
                                    "breaching": c.evaluation.breaching,
                                }
                                for c in output.contributions
                            ],
                        },
                        safety_critical=True,
                    )
                )
                if output.downgraded:
                    notes.append(f"verdict downgraded: {output.downgrade_reason}")
            elif record.tool == "optimise_route":
                findings.append(
                    self._finding(
                        result, step_id, "route", "risk.route",
                        {
                            "distance_km": output.distance_km,
                            "hours": output.estimated_hours,
                            "max_cell_risk": output.max_cell_risk,
                            "avoids": output.avoids,
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

    def review(self, result, intent: Intent) -> list[AgentRequest]:
        """When the answer is "do not go", ask where the shelter is.

        A fisherman told to stay in is served by a verdict. A fisherman told
        the sea is marginal is served by a verdict *and* somewhere to run to,
        and the risk agent does not know where that is -- the geospatial agent
        does.

        This fires on the verdict, not on the query type. A calm day produces
        no request at all, which is the point: the plan is different because
        the sea is different, not because the question was phrased differently.
        """
        requests: list[AgentRequest] = []
        records = self._my_records(result)

        for step_id, record in records.items():
            output = result.outputs.get(step_id)
            if output is None or record.tool != "compute_risk_score":
                continue
            if output.status is ToolStatus.FAILED:
                continue
            if output.verdict not in ("no_go", "marginal"):
                continue

            place = self._origin(result)
            if place is None:
                continue

            requests.append(
                AgentRequest(
                    from_agent=self.name,
                    to_agent="GeospatialAgent",
                    tool="nearest_landing_centre",
                    args={"lat": place[0], "lon": place[1]},
                    reason=(
                        f"verdict is {output.verdict}"
                        + (f" ({output.downgrade_reason})" if output.downgrade_reason else "")
                        + "; the answer should name somewhere to shelter"
                    ),
                    critical=True,
                )
            )
        return requests

    @staticmethod
    def _origin(result) -> tuple[float, float] | None:
        """The resolved coordinate, found by tool rather than by step id."""
        for record in result.tool_call_log.values():
            if record.tool != "resolve_place" or not record.step_id:
                continue
            output = result.outputs.get(record.step_id)
            if output is not None and getattr(output, "lat", None) is not None:
                return (output.lat, output.lon)
        return None


risk_agent = register_agent(RiskAgent())
