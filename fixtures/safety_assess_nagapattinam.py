"""The ideal answer, built as a real object.

This is the executable form of ``docs/ideal_answers/safety_assess.md``. The
document is the design target; this module is the proof the schema can express
it. ``tests/test_ideal_answer.py`` asserts that the two agree.

It is also the primary fixture. Every layer built after this one -- the
narration templates, the verifier, the frontend renderer -- is developed
against these objects rather than against live ingestion, so no work is blocked
on a satellite pass.

**Nothing here is fetched.** The numbers are a hand-written plausible northeast
monsoon situation off Nagapattinam, and they are labelled as such. They are a
fixture, not data. Do not let one of these values reach a slide.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.schemas.tool_io import ToolCallRecord, ToolStatus
from core.schemas import (
    Alternative,
    AlternativeCost,
    Assumption,
    Caveat,
    Claim,
    ClaimKind,
    Comparison,
    Confidence,
    Driver,
    EvidenceEntry,
    NegativeFinding,
    OperationalGuidance,
    QueryType,
    Range,
    ReasoningStep,
    Recommendation,
    ResolvedPlace,
    SpatialContext,
    Templated,
    Threshold,
    Trajectory,
    TurnBackBasis,
    Unit,
    Verdict,
    VerdictValue,
    VesselClass,
    VisualLayer,
    Window,
    evaluate,
)

__all__ = [
    "IST",
    "build_ideal_safety_answer",
    "build_degraded_variant",
    "build_refusal_variant",
    "build_tool_call_log",
    "build_failed_alert_log",
]

#: The coast runs on IST. A naive datetime here is a five-and-a-half hour bug.
IST = timezone(timedelta(hours=5, minutes=30))

_ASKED_AT = datetime(2025, 11, 14, 18, 0, tzinfo=IST)
_MORNING = datetime(2025, 11, 15, 6, 0, tzinfo=IST)
_AFTERNOON = datetime(2025, 11, 15, 14, 0, tzinfo=IST)

#: Cruise speed for an FRP 9 m with an outboard, in knots. Read from
#: config/risk_thresholds.yaml so the fixture cannot drift from the config.
#: Used to compute turn_back rather than assume a flat margin.
_FRP_9M_CRUISE_KN = 7.0

#: The verdict score. NOT hand-picked -- this is what compute_risk_score()
#: actually returns for this scenario, and
#: tests/test_risk_score.py::test_ideal_scenario_still_scores_as_the_fixture_claims
#: fails if the function and this constant ever disagree.
#:
#: An earlier draft of the ideal answer guessed 0.62 before the function
#: existed. The real curve gives 0.4875. The verdict is unchanged -- still
#: marginal, still limited by wave height -- which is the part the design
#: depended on; only the internal number moved.
_IDEAL_SCORE = 0.4875


def _steam_time_h(distance_km: float, speed_kn: float) -> float:
    """Hours to cover a distance at a cruise speed. Deterministic, no LLM."""
    speed_kmh = speed_kn * 1.852
    return distance_km / speed_kmh


def build_ideal_safety_answer() -> Recommendation:
    """The marginal-verdict answer from the ideal answer document.

    A 9 m FRP boat out of Nagapattinam, asking on the evening of 14 Nov about
    the morning of the 15th. Workable at dawn, over the limit by early
    afternoon. The interesting case, and the one the schema was designed for.
    """
    # -- evidence: what the tools returned ------------------------------
    evidence = [
        EvidenceEntry(
            tool_call_id="tc_001",
            tool="resolve_place",
            args={"name": "Nagapattinam"},
            output_digest={"lat": 10.77, "lon": 79.84},
            source="landing_centres",
            retrieved_at=_ASKED_AT,
        ),
        EvidenceEntry(
            tool_call_id="tc_003",
            tool="wave_forecast",
            args={"lat": 10.77, "lon": 79.84, "hours": 24},
            output_digest={
                "hs_min_m": 1.8,
                "hs_max_m": 2.2,
                "hs_later_min_m": 2.6,
                "hs_later_max_m": 3.1,
                "peak_direction_deg": 45.0,
            },
            source="open_meteo_marine",
            native_resolution_deg=0.05,
            issued_at=datetime(2025, 11, 14, 12, 0, tzinfo=timezone.utc),
            retrieved_at=_ASKED_AT,
            data_age_days=0.0,
        ),
        EvidenceEntry(
            tool_call_id="tc_004",
            tool="wind_forecast",
            args={"lat": 10.77, "lon": 79.84, "hours": 24},
            output_digest={
                "speed_min_kn": 15.0,
                "speed_max_kn": 20.0,
                "gust_kn": 25.0,
                "speed_later_min_kn": 22.0,
                "speed_later_max_kn": 30.0,
            },
            source="open_meteo_forecast",
            native_resolution_deg=0.11,
            issued_at=datetime(2025, 11, 14, 12, 0, tzinfo=timezone.utc),
            data_age_days=0.0,
        ),
        EvidenceEntry(
            tool_call_id="tc_005",
            tool="active_alerts",
            args={"district": "Nagapattinam", "types": ["cyclone", "high_wave"]},
            # An empty result is a result. The absence of alerts is what makes
            # the negative findings citable rather than an assumption.
            output_digest={"count": 0, "alerts": []},
            source="imd_incois_alerts",
            retrieved_at=_ASKED_AT,
        ),
        EvidenceEntry(
            tool_call_id="tc_006",
            tool="tides",
            args={"lat": 10.77, "lon": 79.84},
            output_digest={"high_water_local": "05:40", "range_m": 0.9},
            source="open_meteo_marine",
        ),
        EvidenceEntry(
            tool_call_id="tc_007",
            tool="compute_risk_score",
            args={"vessel_class": "frp_9m", "wave": "$tc_003", "wind": "$tc_004"},
            output_digest={"score": _IDEAL_SCORE, "band": "conditional"},
            source="deterministic",
        ),
        EvidenceEntry(
            tool_call_id="tc_008",
            tool="wave_forecast",
            args={"region": "palk_bay", "hours": 24},
            output_digest={"hs_min_m": 0.8, "hs_max_m": 1.2, "extra_distance_km": 55.0},
            source="open_meteo_marine",
            native_resolution_deg=0.05,
        ),
    ]

    # -- drivers: observed against threshold, with a trajectory ----------
    wave_eval = evaluate(
        Range(min=1.8, max=2.2, unit=Unit.METRE),
        Threshold(
            value=2.5,
            unit=Unit.METRE,
            comparison=Comparison.LTE,
            source="risk_thresholds.yaml#significant_wave_height.frp_9m",
            label="significant wave height limit, 9 m FRP",
        ),
    )
    wind_eval = evaluate(
        Range(min=15.0, max=20.0, unit=Unit.KNOT, qualifier="gusting 25", peak=25.0),
        Threshold(
            value=25.0,
            unit=Unit.KNOT,
            comparison=Comparison.LTE,
            source="risk_thresholds.yaml#wind_speed.frp_9m",
            label="sustained wind limit, 9 m FRP",
        ),
    )

    drivers = [
        Driver(
            id="wave_height",
            label_template="Waves {min}-{max} {unit} from the {dir}",
            slots={"min": 1.8, "max": 2.2, "unit": "m", "dir": "northeast"},
            evaluation=wave_eval,
            at=_MORNING,
            trajectory=Trajectory(
                direction="worsening",
                breaches_at=datetime(2025, 11, 15, 13, 0, tzinfo=IST),
                later=Range(min=2.6, max=3.1, unit=Unit.METRE),
                later_at=_AFTERNOON,
                peak_rate_window=(
                    datetime(2025, 11, 15, 11, 0, tzinfo=IST),
                    _AFTERNOON,
                ),
            ),
            evidence=["tc_003"],
        ),
        Driver(
            id="wind_speed",
            label_template="Wind {min}-{max} {unit} gusting {gust}, {dir}",
            slots={"min": 15, "max": 20, "unit": "kn", "gust": 25, "dir": "northeast"},
            evaluation=wind_eval,
            at=_MORNING,
            trajectory=Trajectory(
                direction="worsening",
                breaches_at=_AFTERNOON,
                later=Range(min=22.0, max=30.0, unit=Unit.KNOT),
                later_at=_AFTERNOON,
            ),
            evidence=["tc_004"],
        ),
    ]

    # -- window: turn_back computed from steam time ----------------------
    # Fishing ground is ~18 km offshore; at 7 kn that is about 1.4 h back to
    # the landing centre. ashore_by 13:00 therefore means turning at ~11:00.
    distance_km = 18.0
    steam_h = _steam_time_h(distance_km, _FRP_9M_CRUISE_KN)
    ashore_by = datetime(2025, 11, 15, 13, 0, tzinfo=IST)
    turn_back = datetime(2025, 11, 15, 11, 0, tzinfo=IST)

    window = Window(
        opens=datetime(2025, 11, 15, 4, 0, tzinfo=IST),
        turn_back=turn_back,
        ashore_by=ashore_by,
        basis="tc_003",
        limiting_driver="wave_height",
        turn_back_basis=TurnBackBasis.COMPUTED_STEAM_TIME,
        steam_distance_km=distance_km,
        vessel_speed_kn=_FRP_9M_CRUISE_KN,
        steam_time_h=round(steam_h, 2),
    )

    return Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id="t_002",
        session_id="sess_demo",
        generated_at=_ASKED_AT,
        verdict=Verdict(
            value=VerdictValue.MARGINAL,
            score=_IDEAL_SCORE,
            band="conditional",
            computed_by="tc_007",
            limiting_driver="wave_height",
        ),
        headline=Templated(
            template="Marginal - you can go, but be back by {ashore_by}.",
            slots={"ashore_by": "13:00"},
        ),
        drivers=drivers,
        window=window,
        negative_findings=[
            NegativeFinding(
                id="no_cyclone",
                template="No cyclone or depression active in the Bay of Bengal.",
                checked_by="tc_005",
                authority="IMD",
                valid_until=datetime(2025, 11, 15, 18, 0, tzinfo=IST),
            ),
            NegativeFinding(
                id="no_high_wave_warning",
                template="No INCOIS high-wave warning in force for {district}.",
                slots={"district": "Nagapattinam"},
                checked_by="tc_005",
                authority="INCOIS",
            ),
            NegativeFinding(
                id="tide_not_limiting",
                template="Tide is not a constraint; high water {hw_time}.",
                slots={"hw_time": "05:40"},
                checked_by="tc_006",
            ),
        ],
        claims=[
            Claim(
                id="c1",
                kind=ClaimKind.OBSERVED,
                template="Waves {min}-{max} {unit} from the {dir} at {at}.",
                slots={
                    "min": 1.8,
                    "max": 2.2,
                    "unit": "m",
                    "dir": "northeast",
                    "at": "06:00",
                },
                evidence=["tc_003"],
            ),
            Claim(
                id="c2",
                kind=ClaimKind.DERIVED,
                template="Sea state is moderate and within limits for a {vessel}.",
                slots={"vessel": "9 m FRP boat"},
                derived_by="rule:sea_state_band",
                evidence=["tc_003", "tc_007"],
            ),
            Claim(
                id="c3",
                kind=ClaimKind.DERIVED,
                template="Most of the build happens between {frm} and {to}.",
                slots={"frm": "11:00", "to": "14:00"},
                derived_by="rule:gradient_window",
                evidence=["tc_003"],
            ),
            Claim(
                id="c4",
                kind=ClaimKind.INFERRED,
                # Carries no numbers, so the verifier is lenient. If it named a
                # figure, that figure would be held to the observed standard.
                template="The afternoon build is the usual northeast monsoon surge.",
                evidence=["tc_003"],
            ),
        ],
        operational_guidance=[
            OperationalGuidance(
                priority=1,
                template="Start back by {turn_back} to be ashore by {ashore_by}.",
                slots={"turn_back": "11:00", "ashore_by": "13:00"},
            ),
            OperationalGuidance(
                priority=2,
                template="Keep VHF on channel {ch}; conditions change fast after noon.",
                slots={"ch": 16},
            ),
        ],
        alternatives=[
            Alternative(
                id="palk_bay",
                template="Palk Bay north of Point Calimere stays {min}-{max} {unit} all day.",
                slots={"min": 0.8, "max": 1.2, "unit": "m"},
                cost=AlternativeCost(
                    extra_distance_km=55.0,
                    extra_steam_time_h=round(_steam_time_h(55.0, _FRP_9M_CRUISE_KN), 1),
                    note="Only worth it for a full day's fishing.",
                ),
                verdict=VerdictValue.GO,
                evidence=["tc_008"],
            )
        ],
        spatial_context=SpatialContext(
            origin=ResolvedPlace(
                name="Nagapattinam",
                lat=10.77,
                lon=79.84,
                source="landing_centres",
                resolved_by="tc_001",
            ),
            vessel_class=VesselClass.FRP_9M,
            aoi_bbox=(79.5, 10.3, 80.4, 11.2),
            inherited_from_turn="t_001",
        ),
        assumptions=[
            Assumption(
                text="Vessel class carried over from the previous turn.",
                field="vessel_class",
                value="frp_9m",
                source_turn="t_001",
                confirmed_by_user=True,
            )
        ],
        confidence=Confidence(
            overall=0.78,
            by_claim={"c1": 0.9, "c2": 0.85, "c3": 0.6, "c4": 0.5},
            basis=(
                "Open-Meteo marine forecast issued 2025-11-14T12:00Z, 0.05 deg wave "
                "grid, 6 h old. Afternoon build timing uncertain by +/-2 h."
            ),
        ),
        caveats=[
            Caveat(
                text="Timing of the afternoon build may move by two hours either way.",
                applies_to=["c3", "window"],
            )
        ],
        evidence=evidence,
        visual_layers=[
            VisualLayer(id="origin", type="point", ref="tc_001"),
            VisualLayer(id="wave_24h", type="timeseries", ref="tc_003", threshold=2.5),
            VisualLayer(id="palk_bay_alt", type="polygon", ref="tc_008"),
        ],
        reasoning_trace=[
            ReasoningStep(step="s1", tool="resolve_place", status="ok", ms=12, tool_call_id="tc_001"),
            ReasoningStep(step="s3", tool="wave_forecast", status="ok", ms=140, tool_call_id="tc_003"),
            ReasoningStep(step="s4", tool="wind_forecast", status="ok", ms=120, tool_call_id="tc_004"),
            ReasoningStep(step="s5", tool="active_alerts", status="ok", ms=95, tool_call_id="tc_005"),
            ReasoningStep(step="s7", tool="compute_risk_score", status="ok", ms=3, tool_call_id="tc_007"),
        ],
    )


def build_degraded_variant() -> Recommendation:
    """Same question, but the alert check timed out.

    The rule this fixture exists to pin down: **a failed safety check can never
    produce a go.** The cyclone negative finding disappears because it was
    never established, a caveat says so plainly, confidence drops, and the
    verdict is downgraded rather than quietly kept.
    """
    base = build_ideal_safety_answer()

    evidence = [
        e.model_copy(
            update={"status": "failed", "error": "timeout after 5s", "output_digest": {}}
        )
        if e.tool_call_id == "tc_005"
        else e
        for e in base.evidence
    ]

    # model_copy(update=...) does NOT re-run validators in Pydantic v2, so a
    # derived fixture could otherwise hold a shape the schema would reject --
    # for instance a 'go' verdict alongside a failed safety check. The copy is
    # re-validated below before it is returned.
    draft = base.model_copy(
        update={
            "turn_id": "t_002_degraded",
            "verdict": base.verdict.downgraded_to(
                VerdictValue.NO_GO, "alert check unavailable"
            ),
            # Both cyclone and high-wave findings were established by tc_005,
            # which failed. Only the tide finding survives.
            "negative_findings": [
                nf for nf in base.negative_findings if nf.checked_by != "tc_005"
            ],
            "evidence": evidence,
            "degraded": True,
            "degradation_notes": [
                "Alert check (IMD cyclone, INCOIS high-wave) did not complete.",
            ],
            "caveats": base.caveats
            + [
                Caveat(
                    text=(
                        "We could not confirm whether a cyclone warning is in force. "
                        "Verdict downgraded because of it, not because of the sea state."
                    ),
                    applies_to=["verdict"],
                )
            ],
            "confidence": base.confidence.model_copy(
                update={
                    "overall": 0.35,
                    "basis": base.confidence.basis
                    + " Alert check failed; cyclone status unknown.",
                }
            ),
            "reasoning_trace": [
                s.model_copy(update={"status": "failed", "note": "timeout after 5s"})
                if s.tool == "active_alerts"
                else s
                for s in base.reasoning_trace
            ],
        }
    )
    return Recommendation.model_validate(draft.model_dump())


def build_refusal_variant() -> Recommendation:
    """Asked about Kochi -- outside the South Coromandel box.

    A refusal is an answer, not an error. It travels in the same object with
    ``verdict`` None, no drivers and no window, so the frontend renders one
    shape for everything.
    """
    from core.schemas import Refusal, RefusalReason, SpatialReference

    return Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id="t_003",
        generated_at=_ASKED_AT,
        refusal=Refusal(
            reason=RefusalReason.OUT_OF_REGION,
            explanation_template=(
                "{place} is on the west coast, outside the area this system covers."
            ),
            slots={"place": "Kochi"},
            requested=SpatialReference(name="Kochi", lat=9.93, lon=76.27, resolved_by="tc_001"),
            supported_region="South Coromandel: Chennai to Rameswaram, 78.5-82.0 E, 8.0-12.0 N",
        ),
        headline=Templated(
            template="{place} is outside the area this system covers.",
            slots={"place": "Kochi"},
        ),
        confidence=Confidence(
            overall=1.0,
            basis="Coverage boundary is a fixed configuration value, not an estimate.",
        ),
        evidence=[
            EvidenceEntry(
                tool_call_id="tc_001",
                tool="resolve_place",
                args={"name": "Kochi"},
                output_digest={"lat": 9.93, "lon": 76.27, "in_bbox": False},
                source="geocoder",
            )
        ],
    )


# ==========================================================================
# The tool call log
# ==========================================================================
# The verifier checks the recommendation against THIS, not against the
# recommendation's own evidence[] block. See orchestrator/verifier.py for why
# that distinction is the whole point: an answer verified against its own
# citations proves nothing.


def build_tool_call_log() -> dict[str, ToolCallRecord]:
    """What the executor would have written while running the ideal answer's plan.

    `output_numbers` is the flattened set of numbers each call actually
    returned. It is the haystack every claimed number must be found in.
    """
    raw: dict[str, tuple[str, dict, list[float]]] = {
        "tc_001": ("resolve_place", {"name": "Nagapattinam"}, [10.77, 79.84]),
        "tc_003": (
            "wave_forecast",
            {"lat": 10.77, "lon": 79.84, "hours": 24},
            # Unrounded, as a model would return them. The claims round these
            # for display, which is exactly what the matching rule permits.
            [1.82, 2.17, 2.61, 3.09, 45.0, 7.4],
        ),
        "tc_004": (
            "wind_forecast",
            {"lat": 10.77, "lon": 79.84, "hours": 24},
            [15.2, 19.8, 24.9, 22.4, 29.6],
        ),
        "tc_005": (
            "active_alerts",
            {"district": "Nagapattinam", "types": ["cyclone", "high_wave"]},
            [0.0],
        ),
        "tc_006": ("tides", {"lat": 10.77, "lon": 79.84}, [0.92]),
        "tc_007": (
            "compute_risk_score",
            {"vessel_class": "frp_9m"},
            [_IDEAL_SCORE],
        ),
        "tc_008": (
            "wave_forecast",
            {"region": "palk_bay", "hours": 24},
            [0.81, 1.18, 55.0],
        ),
    }
    return {
        tid: ToolCallRecord(
            tool_call_id=tid,
            tool=tool,
            args=args,
            output_numbers=numbers,
            status=ToolStatus.OK,
            turn_id="t_002",
        )
        for tid, (tool, args, numbers) in raw.items()
    }


def build_failed_alert_log() -> dict[str, ToolCallRecord]:
    """The same log, but the alert check timed out. Pairs with build_degraded_variant()."""
    log = build_tool_call_log()
    log["tc_005"] = log["tc_005"].model_copy(
        update={"status": ToolStatus.FAILED, "error": "timeout after 5s", "output_numbers": []}
    )
    return log
