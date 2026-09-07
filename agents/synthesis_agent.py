"""Assembles the recommendation object from what the executor produced.

Despite the name, **there is no LLM in this file**. Every block below is built
deterministically from typed tool outputs, because every one of them carries a
number attached to a safety claim, and the governing rule puts those in code.

What the LLM will eventually do here (Phase 3) is narrate: take the finished
object and turn slot templates into fluent English, or Tamil. It will not
choose a verdict, compute a window, or decide which drivers mattered. Those are
decided here, from tool output, and the verifier checks the result against the
tool call log afterwards.

The window calculation is the interesting part. "Be back by 13:00" is not in
any tool's output -- it is derived from when the limiting driver crosses its
threshold, minus the time it takes to steam home. See :func:`_build_window`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import config
from core.schemas.intent import Intent, QueryType, VesselClass
from agents.hypotheses import propose as propose_hypotheses
from core.units import Range, Unit
from core.schemas.recommendation import (
    Alternative,
    AlternativeCost,
    Assumption,
    Caveat,
    Claim,
    ClaimKind,
    Confidence,
    Driver,
    Hypothesis,
    EvidenceEntry,
    NegativeFinding,
    OperationalGuidance,
    Recommendation,
    ResolvedPlace,
    SpatialContext,
    Templated,
    Trajectory,
    TurnBackBasis,
    Verdict,
    VerdictValue,
    VisualLayer,
    Window,
)
from core.schemas.tool_io import ToolStatus
from orchestrator.executor import ExecutionResult
from rag.store import background_for_causal

__all__ = ["build_recommendation", "KM_PER_KNOT_HOUR"]

IST = timezone(timedelta(hours=5, minutes=30))

#: One knot is one nautical mile (1852 m) per hour.
KM_PER_KNOT_HOUR = 1.852

#: Below this, a verdict is reported without a window -- there is nothing
#: useful to say about timing if the workable period is under an hour.
_MIN_USEFUL_WINDOW_H = 1.0


def _evidence_entries(result: ExecutionResult) -> list[EvidenceEntry]:
    """Turn the tool call log into the answer's evidence block.

    Built from the log rather than assembled by hand, so the two cannot
    disagree -- which is what the verifier would otherwise catch as fabricated
    evidence.
    """
    entries: list[EvidenceEntry] = []
    for record in sorted(result.tool_call_log.values(), key=lambda r: r.tool_call_id):
        output = record.output or {}
        provenance = output.get("provenance") or {}
        quality = output.get("quality") or {}

        # Keep scalars; drop the bulk series. A 24-hour timeseries per turn is
        # not something to store, and nothing quotes it directly.
        digest = {
            k: v
            for k, v in output.items()
            if k not in ("provenance", "quality", "status", "error", "series")
            and not isinstance(v, (list, dict))
        }
        for key in ("significant_wave_height", "wind_speed", "wave_period", "visibility"):
            band = output.get(key)
            if isinstance(band, dict):
                digest[key] = {
                    k: v for k, v in band.items() if k in ("min", "max", "peak")
                }

        entries.append(
            EvidenceEntry(
                tool_call_id=record.tool_call_id,
                tool=record.tool,
                args=record.args,
                output_digest=digest,
                source=provenance.get("source") or "unknown",
                retrieved_at=_dt(provenance.get("retrieved_at")),
                issued_at=_dt(provenance.get("issued_at")),
                native_resolution_deg=provenance.get("native_resolution_deg"),
                data_age_days=quality.get("data_age_days"),
                clear_pass_fraction=quality.get("clear_pass_fraction"),
                composite_window_days=quality.get("composite_window_days"),
                status=record.status.value,
                error=record.error,
            )
        )
    return entries


def _dt(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value


def _first_breach(series, threshold_value: float):
    """When the wave series first crosses the limit, and how fast it builds.

    Returns (breach_time, later_sample, peak_rate_window). The peak-rate window
    is what produces "most of the build happens between 11:00 and 14:00", which
    is the sentence that actually changes behaviour -- knowing conditions
    worsen is far less useful than knowing how suddenly.
    """
    breach_at = None
    for sample in series:
        if sample.significant_height_m > threshold_value:
            breach_at = sample.time
            break

    steepest = None
    best_rate = 0.0
    for a, b in zip(series, series[1:]):
        hours = (b.time - a.time).total_seconds() / 3600.0
        if hours <= 0:
            continue
        rate = (b.significant_height_m - a.significant_height_m) / hours
        if rate > best_rate:
            best_rate, steepest = rate, (a.time, b.time)

    later = max(series, key=lambda s: s.significant_height_m) if series else None
    return breach_at, later, steepest


def _build_window(
    result: ExecutionResult,
    vessel_class: str,
    threshold_value: float,
) -> Window | None:
    """Derive the workable window and when to turn back.

    Nothing in any tool output says "be back by 13:00". It is computed:

        ashore_by  = when the limiting driver crosses its threshold
        turn_back  = ashore_by - (operating range / cruise speed)
        opens      = the start of the forecast window

    The steam time is real arithmetic over the vessel's cruise speed and
    typical operating range, both from config -- which is why the user asked
    for a computable turn-back rather than a flat margin. Both inputs are
    currently tagged provisional in ``risk_thresholds.yaml``, and that tag
    matters here more than anywhere else: this number decides how early a man
    is told to start for home.

    Returns None when the conditions never breach, or when the window is too
    short to be worth stating.
    """
    wave = result.output_for("s2")
    basis = result.call_id_for("s2")
    if wave is None or not wave.series or basis is None:
        return None

    breach_at, _, _ = _first_breach(wave.series, threshold_value)
    if breach_at is None:
        return None  # workable throughout; no window to close

    spec = config.vessel_spec(vessel_class)
    speed_kn = float(spec["cruise_speed_kn"])
    range_km = float(spec.get("typical_operating_range_km", 20.0))
    steam_h = range_km / (speed_kn * KM_PER_KNOT_HOUR)

    opens = wave.series[0].time
    ashore_by = breach_at
    turn_back = ashore_by - timedelta(hours=steam_h)

    if (turn_back - opens).total_seconds() / 3600.0 < _MIN_USEFUL_WINDOW_H:
        return None

    return Window(
        opens=opens,
        turn_back=turn_back,
        ashore_by=ashore_by,
        basis=basis,
        limiting_driver="significant_wave_height",
        turn_back_basis=TurnBackBasis.COMPUTED_STEAM_TIME,
        steam_distance_km=range_km,
        vessel_speed_kn=speed_kn,
        steam_time_h=round(steam_h, 2),
    )


def _drivers(result: ExecutionResult, risk, threshold_value: float) -> list[Driver]:
    """One driver per contribution the risk function actually weighed."""
    wave = result.output_for("s2")
    evidence_for = {
        "significant_wave_height": result.call_id_for("s2"),
        "wave_steepness": result.call_id_for("s2"),
        "wind_speed": result.call_id_for("s3"),
        "visibility": result.call_id_for("s3"),
    }
    labels = {
        "significant_wave_height": "Waves {min}-{max} {unit}",
        "wind_speed": "Wind {min}-{max} {unit}",
        "visibility": "Visibility {min}-{max} {unit}",
        "wave_steepness": "Wave steepness {max}",
    }

    trajectory = None
    if wave is not None and wave.series:
        breach_at, later, steepest = _first_breach(wave.series, threshold_value)
        first = wave.series[0].significant_height_m
        last = wave.series[-1].significant_height_m
        direction = (
            "worsening" if last > first + 0.1
            else "improving" if last < first - 0.1
            else "steady"
        )
        from core.units import Range, Unit

        trajectory = Trajectory(
            direction=direction,
            breaches_at=breach_at,
            later=Range(
                min=later.significant_height_m, max=later.significant_height_m, unit=Unit.METRE
            )
            if later
            else None,
            later_at=later.time if later else None,
            peak_rate_window=steepest,
        )

    drivers: list[Driver] = []
    for contribution in risk.contributions:
        call_id = evidence_for.get(contribution.driver_id)
        if call_id is None:
            continue
        observed = contribution.evaluation.observed
        drivers.append(
            Driver(
                id=contribution.driver_id,
                label_template=labels.get(contribution.driver_id, "{min}-{max} {unit}"),
                slots={
                    "min": round(observed.min, 2),
                    "max": round(observed.max, 2),
                    "unit": observed.unit.value,
                },
                evaluation=contribution.evaluation,
                at=wave.series[0].time if wave and wave.series else datetime.now(IST),
                trajectory=trajectory if contribution.driver_id == "significant_wave_height" else None,
                evidence=[call_id],
            )
        )
    return drivers


def _negative_findings(result: ExecutionResult) -> list[NegativeFinding]:
    """Only findings we actually established.

    A failed alert check produces NO negative finding. "We could not check" is
    not "there is nothing there", and the verifier rejects a negative finding
    resting on a failed call -- so emitting one here would fail verification,
    which is the check working as intended.
    """
    findings: list[NegativeFinding] = []

    alerts = result.output_for("s5")
    alerts_id = result.call_id_for("s5")
    if alerts is not None and alerts.status is ToolStatus.OK and alerts.count == 0:
        findings.append(
            NegativeFinding(
                id="no_cyclone",
                template="No cyclone or depression active in the Bay of Bengal.",
                checked_by=alerts_id,
                authority="IMD",
            )
        )

    tide = result.output_for("s4")
    tide_id = result.call_id_for("s4")
    if tide is not None and tide.status is ToolStatus.OK and not tide.is_limiting:
        high = next((e for e in tide.extremes if e.kind == "high"), None)
        findings.append(
            NegativeFinding(
                id="tide_not_limiting",
                template="Tide is not a constraint{detail}.",
                slots={"detail": f"; high water {high.time.strftime('%H:%M')}" if high else ""},
                checked_by=tide_id,
            )
        )
    return findings


_VERDICT_HEADLINES = {
    VerdictValue.GO: "Safe to go out.",
    VerdictValue.MARGINAL: "Marginal - you can go, but watch the time.",
    VerdictValue.NO_GO: "Do not go out.",
}


def build_recommendation(
    result: ExecutionResult,
    intent: Intent,
    turn_id: str = "t_001",
) -> Recommendation:
    """Assemble the answer. Deterministic; every number comes from a tool."""
    # The other three builders live at the bottom of this module. Only
    # safety_assess produces a verdict; see the note above _build_pfz for why.
    if intent.query_type is QueryType.PFZ_LOCATE:
        return _build_pfz(result, intent, turn_id)
    if intent.query_type is QueryType.GEOFENCE_CHECK:
        return _build_geofence(result, intent, turn_id)
    if intent.query_type is QueryType.CAUSAL_EXPLAIN:
        return _build_causal(result, intent, turn_id)
    if intent.query_type is QueryType.CONDITIONS_REPORT:
        return _build_conditions(result, intent, turn_id)

    vessel_class = (intent.vessel_class or VesselClass.FRP_9M).value
    risk = result.output_for("s6")
    place = result.output_for("s1")
    evidence = _evidence_entries(result)

    if risk is None:
        # Nothing to adjudicate on. Rather than inventing a verdict, say so.
        return Recommendation(
            query_type=QueryType.SAFETY_ASSESS,
            turn_id=turn_id,
            generated_at=datetime.now(IST),
            headline=Templated(
                template="Could not assess conditions - the safety calculation did not run."
            ),
            evidence=evidence,
            degraded=True,
            degradation_notes=result.degradation_notes,
            confidence=Confidence(
                overall=0.0,
                basis="The risk calculation did not run; no verdict was reached.",
            ),
            reasoning_trace=result.trace,
        )

    verdict_value = VerdictValue(risk.verdict)
    threshold = config.thresholds_for(vessel_class)["significant_wave_height"].value

    verdict = Verdict(
        value=verdict_value,
        score=risk.score,
        band=risk.band,
        computed_by=result.call_id_for("s6"),
        limiting_driver=risk.limiting_driver,
    )

    drivers = _drivers(result, risk, threshold)
    window = _build_window(result, vessel_class, threshold)
    findings = _negative_findings(result)

    claims: list[Claim] = []
    wave = result.output_for("s2")
    if wave is not None and result.call_id_for("s2"):
        claims.append(
            Claim(
                id="c1",
                kind=ClaimKind.OBSERVED,
                template="Waves {min}-{max} {unit}.",
                slots={
                    "min": round(wave.significant_wave_height.min, 2),
                    "max": round(wave.significant_wave_height.max, 2),
                    "unit": "m",
                },
                evidence=[result.call_id_for("s2")],
            )
        )
    wind = result.output_for("s3")
    if wind is not None and result.call_id_for("s3"):
        claims.append(
            Claim(
                id="c2",
                kind=ClaimKind.OBSERVED,
                template="Wind {min}-{max} {unit}.",
                slots={
                    "min": round(wind.wind_speed.min, 2),
                    "max": round(wind.wind_speed.max, 2),
                    "unit": "kn",
                },
                evidence=[result.call_id_for("s3")],
            )
        )

    guidance: list[OperationalGuidance] = []
    # The downgrade reason, when there is one, outranks the timing advice: a
    # fisherman needs to know WHY he is being told to stay in before he is told
    # when to be back. Priority 1 is the highest the schema allows.
    if risk.downgraded and risk.downgrade_reason:
        guidance.append(
            OperationalGuidance(
                priority=1,
                template="Verdict downgraded: {reason}.",
                slots={"reason": risk.downgrade_reason},
            )
        )
    if window is not None:
        guidance.append(
            OperationalGuidance(
                priority=2,
                template="Start back by {turn_back} to be ashore by {ashore_by}.",
                slots={
                    "turn_back": window.turn_back.strftime("%H:%M"),
                    "ashore_by": window.ashore_by.strftime("%H:%M"),
                },
            )
        )
    caveats = [Caveat(text=note, applies_to=["verdict"]) for note in result.degradation_notes]

    quality_notes = [
        f"{e.tool} {e.source}"
        + (f", {e.data_age_days:.1f} days old" if e.data_age_days else "")
        for e in evidence
        if e.status == "ok"
    ]
    basis = "; ".join(quality_notes) or "No successful data source."
    if risk.downgraded:
        basis += f". Verdict downgraded: {risk.downgrade_reason}."

    spatial = None
    if place is not None and place.status is ToolStatus.OK:
        spatial = SpatialContext(
            origin=ResolvedPlace(
                name=place.matched_name,
                lat=place.lat,
                lon=place.lon,
                source=place.provenance.source,
                resolved_by=result.call_id_for("s1"),
            ),
            vessel_class=VesselClass(vessel_class),
        )

    assumptions: list[Assumption] = []
    spec = config.vessel_spec(vessel_class)
    if window is not None:
        assumptions.append(
            Assumption(
                text=(
                    f"Turn-back time assumes a {spec['typical_operating_range_km']} km "
                    f"operating range at {spec['cruise_speed_kn']} kn. Both are "
                    "provisional and not yet validated against local data."
                ),
                field="typical_operating_range_km",
                value=spec["typical_operating_range_km"],
            )
        )

    layers = [
        VisualLayer(id="origin", type="point", ref=result.call_id_for("s1"))
        if result.call_id_for("s1")
        else None,
        VisualLayer(
            id="wave_24h", type="timeseries", ref=result.call_id_for("s2"), threshold=threshold
        )
        if result.call_id_for("s2")
        else None,
    ]

    return Recommendation(
        query_type=QueryType.SAFETY_ASSESS,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        verdict=verdict,
        headline=Templated(template=_VERDICT_HEADLINES[verdict_value]),
        claims=claims,
        drivers=drivers,
        negative_findings=findings,
        operational_guidance=guidance,
        window=window,
        spatial_context=spatial,
        assumptions=assumptions,
        confidence=Confidence(
            overall=0.2 if result.degraded else 0.8,
            by_claim={c.id: 0.85 for c in claims},
            basis=basis,
        ),
        caveats=caveats,
        evidence=evidence,
        visual_layers=[layer for layer in layers if layer is not None],
        reasoning_trace=result.trace,
        degraded=result.degraded,
        degradation_notes=result.degradation_notes,
    )


# ==========================================================================
# The other three query types
# ==========================================================================
#
# Added 2026-09-06, once the ocean tools were live. Until then `safety_assess`
# was the only path with data behind it and the others raised NotImplementedError.
#
# Three properties hold across all of them, and they are what make these more
# than string formatting:
#
# 1.  **No verdict outside safety_assess.** `Recommendation.verdict` stays None
#     for pfz, geofence and causal. A verdict is a go/no-go safety adjudication
#     and `compute_risk_score` is its only legitimate source; attaching one to
#     "here is where the fish are" would smuggle a safety claim into an answer
#     that never assessed safety.
# 2.  **Every number in a claim comes from a tool output and cites its call.**
#     The verifier walks these exactly as it walks the safety ones.
# 3.  **An empty result is a negative finding, not a gap.** No PFZ candidate
#     within range is an answer. So is "clear of every zone we could check".


def _spatial_from_place(result: ExecutionResult, intent: Intent, step_id: str = "s1"):
    """SpatialContext from a resolve_place step, or None."""
    place = result.output_for(step_id)
    if place is None or place.status is ToolStatus.FAILED:
        return None
    return SpatialContext(
        origin=ResolvedPlace(
            name=place.matched_name,
            lat=place.lat,
            lon=place.lon,
            source="bbox.yaml#reference_points",
            resolved_by=result.call_id_for(step_id) or "",
        ),
        vessel_class=intent.vessel_class.value if intent.vessel_class else None,
    )


def _place_caveat(result: ExecutionResult, step_id: str = "s1") -> list[Caveat]:
    """The provisional-gazetteer caveat, when it applies."""
    place = result.output_for(step_id)
    if place is None or place.status is ToolStatus.FAILED:
        return []
    if place.match_confidence >= 1.0:
        return []
    return [
        Caveat(
            text=(
                "Position comes from the provisional 7-point gazetteer in "
                "bbox.yaml, not the INCOIS landing centre set."
            )
        )
    ]


def _no_data_answer(
    query_type: QueryType,
    turn_id: str,
    result: ExecutionResult,
    message: str,
) -> Recommendation:
    """A well-formed answer that says the data was not there.

    Preferred over raising: the turn still carries its evidence and its trace,
    so a judge can see exactly which call failed rather than a stack trace.
    """
    return Recommendation(
        query_type=query_type,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        headline=Templated(template=message),
        evidence=_evidence_entries(result),
        reasoning_trace=result.trace,
        degraded=True,
        degradation_notes=result.degradation_notes or [message],
        confidence=Confidence(overall=0.0, basis="required tool did not return"),
    )


def _build_pfz(result: ExecutionResult, intent: Intent, turn_id: str) -> Recommendation:
    """Where the fish are likely to be, and why we think so.

    No verdict: this answer does not adjudicate safety. If a fisherman asks
    where to fish, the honest response is the zone plus its reasoning, and a
    caveat pointing at the safety question rather than a fabricated one.
    """
    pfz = _first_output(result, "pfz_candidates")
    if pfz is None:
        return _no_data_answer(
            QueryType.PFZ_LOCATE, turn_id, result,
            "Could not locate fishing zones - the ocean data did not return.",
        )
    pfz_id = _first_call_id(result, "pfz_candidates")
    claims: list[Claim] = []
    findings: list[NegativeFinding] = []

    if pfz.status is ToolStatus.FAILED:
        return _no_data_answer(
            QueryType.PFZ_LOCATE, turn_id, result,
            f"Could not locate fishing zones: {pfz.error or 'ocean data unavailable'}",
        )

    if not pfz.candidates:
        # A real answer. September on this coast is thermally uniform, and
        # ranking noise so as to have something to say would be worse than
        # saying nothing was found.
        headline = Templated(
            template="No potential fishing zone met the criteria within range today."
        )
        findings.append(
            NegativeFinding(
                id="no_pfz_candidate",
                template=(
                    "No sea-surface temperature front strong enough to aggregate "
                    "fish was found within range."
                ),
                checked_by=pfz_id,
            )
        )
    else:
        best = pfz.candidates[0]
        headline = Templated(
            template="Best fishing zone is {distance_km} km {bearing_deg}° from port.",
            slots={"distance_km": best.distance_km, "bearing_deg": best.bearing_deg},
        )
        claims.append(
            Claim(
                id="pfz_position",
                kind=ClaimKind.DERIVED,
                template="Candidate zone {distance_km} km at bearing {bearing_deg}°.",
                slots={"distance_km": best.distance_km, "bearing_deg": best.bearing_deg},
                evidence=[pfz_id] if pfz_id else [],
                derived_by=pfz_id,
            )
        )
        if best.front_gradient_deg_c_per_km is not None:
            claims.append(
                Claim(
                    id="pfz_front",
                    kind=ClaimKind.OBSERVED,
                    template="Sea-surface temperature changes {gradient} °C per km across the front.",
                    slots={"gradient": best.front_gradient_deg_c_per_km},
                    evidence=[pfz_id] if pfz_id else [],
                )
            )
        if best.chl_mg_m3 is not None:
            claims.append(
                Claim(
                    id="pfz_chl",
                    kind=ClaimKind.OBSERVED,
                    template="Chlorophyll there is {chl} mg/m3.",
                    slots={"chl": best.chl_mg_m3},
                    evidence=[pfz_id] if pfz_id else [],
                )
            )
        # Runner-up zones are reported as claims, not as Alternatives.
        # Alternative.verdict is the safety enum (go/marginal/no_go), and a
        # fishing candidate has no safety verdict -- filling that field would
        # put a safety claim into an answer that never assessed safety.
        for n, candidate in enumerate(pfz.candidates[1:3], start=2):
            claims.append(
                Claim(
                    id=f"pfz_alt_{n}",
                    kind=ClaimKind.DERIVED,
                    template="Another candidate zone lies {distance_km} km at {bearing_deg}°.",
                    slots={
                        "distance_km": candidate.distance_km,
                        "bearing_deg": candidate.bearing_deg,
                    },
                    evidence=[pfz_id] if pfz_id else [],
                    derived_by=pfz_id,
                )
            )

    geofence = _first_output(result, "geofence_check")
    if geofence is not None and geofence.status is not ToolStatus.FAILED and geofence.clear:
        findings.append(
            NegativeFinding(
                id="clear_of_zones",
                template="Departure point is clear of {zones}.",
                slots={"zones": ", ".join(geofence.zones_checked)},
                checked_by=_first_call_id(result, "geofence_check"),
            )
        )

    caveats = _place_caveat(result) + [
        Caveat(
            text=(
                "Derived from satellite fronts and chlorophyll, not from the "
                "official INCOIS PFZ advisory, which was not available to compare."
            )
        ),
        Caveat(
            text=(
                "This answers where to fish, not whether it is safe to sail. "
                "Ask a safety question before departing."
            )
        ),
    ]
    if pfz.quality.is_degraded:
        caveats.append(Caveat(text=f"Satellite data: {pfz.quality.describe()}."))

    return Recommendation(
        query_type=QueryType.PFZ_LOCATE,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        # No verdict. See the note at the top of this section.
        headline=headline,
        claims=claims,
        negative_findings=findings,
        spatial_context=_spatial_from_place(result, intent),
        operational_guidance=[
            OperationalGuidance(
                template="Check the safety forecast before leaving.", priority=1
            )
        ],
        confidence=Confidence(
            overall=0.3 if result.degraded else 0.6,
            by_claim={c.id: 0.6 for c in claims},
            basis=f"{pfz.provenance.source}; {pfz.quality.describe()}",
        ),
        caveats=caveats,
        evidence=_evidence_entries(result),
        visual_layers=[
            VisualLayer(id="pfz_candidates", type="point", ref=pfz_id)
        ] if pfz_id else [],
        reasoning_trace=result.trace,
        degraded=result.degraded,
        degradation_notes=result.degradation_notes,
    )


def _build_geofence(result: ExecutionResult, intent: Intent, turn_id: str) -> Recommendation:
    """Which zones must be avoided.

    The single most consequential honesty point in this builder: **"clear"
    means clear of what was actually checked.** Only the IMBL has geometry
    today; MPA and EEZ do not. Reporting an unqualified "you are clear" would
    be a confident statement about boundaries we never looked at.
    """
    geofence = _first_output(result, "geofence_check")
    if geofence is None or geofence.status is ToolStatus.FAILED:
        return _no_data_answer(
            QueryType.GEOFENCE_CHECK, turn_id, result,
            "Could not check maritime boundaries - the geofence data did not return.",
        )
    geofence_id = _first_call_id(result, "geofence_check")

    claims: list[Claim] = []
    findings: list[NegativeFinding] = []
    guidance: list[OperationalGuidance] = []
    checked = ", ".join(geofence.zones_checked) or "no zones"

    if geofence.clear:
        headline = Templated(
            template="Clear of {zones}.", slots={"zones": checked}
        )
        findings.append(
            NegativeFinding(
                id="clear_of_checked_zones",
                template="No boundary breach against {zones}.",
                slots={"zones": checked},
                checked_by=geofence_id,
                authority="Limits in the Seas No. 77 (1974/1976 treaty text)",
            )
        )
    else:
        nearest = min(geofence.hits, key=lambda h: getattr(h, "distance_km", 0.0))
        headline = Templated(
            template="Inside or close to {zone} - do not cross.",
            slots={"zone": getattr(nearest, "zone_id", "a restricted zone")},
        )
        for n, hit in enumerate(geofence.hits, start=1):
            claims.append(
                Claim(
                    id=f"geofence_hit_{n}",
                    kind=ClaimKind.DERIVED,
                    template="{zone} is {distance_km} km away.",
                    slots={
                        "zone": getattr(hit, "zone_id", "zone"),
                        "distance_km": getattr(hit, "distance_km", None),
                    },
                    evidence=[geofence_id] if geofence_id else [],
                    derived_by=geofence_id,
                )
            )
        guidance.append(
            OperationalGuidance(
                template=(
                    "Crossing the International Maritime Boundary Line risks "
                    "arrest and seizure of the vessel."
                ),
                priority=1,
            )
        )

    missing = [z for z in ("mpa", "eez") if z not in geofence.zones_checked]
    caveats = _place_caveat(result)
    if missing:
        caveats.append(
            Caveat(
                text=(
                    f"No geometry for {', '.join(missing)}. 'Clear' means clear "
                    "of the zones listed, not of every restricted area."
                )
            )
        )

    return Recommendation(
        query_type=QueryType.GEOFENCE_CHECK,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        headline=headline,
        claims=claims,
        negative_findings=findings,
        operational_guidance=guidance,
        spatial_context=_spatial_from_place(result, intent),
        confidence=Confidence(
            overall=0.5 if missing else 0.8,
            by_claim={c.id: 0.9 for c in claims},
            basis=(
                f"{geofence.provenance.source}; zones checked: {checked}"
                + (f"; not checked: {', '.join(missing)}" if missing else "")
            ),
        ),
        caveats=caveats,
        evidence=_evidence_entries(result),
        visual_layers=[
            VisualLayer(id="zones", type="polygon", ref=geofence_id)
        ] if geofence_id else [],
        reasoning_trace=result.trace,
        degraded=result.degraded,
        degradation_notes=result.degradation_notes,
    )


#: Beyond this many standard deviations from the monthly baseline, a
#: chlorophyll reading is treated as supporting a productivity hypothesis.
#: Two sigma is the conventional line. It is a *test threshold*, not a verdict.
_ANOMALY_SUPPORTED_SIGMA = 2.0


def _hypothesis(
    hypothesis_id: str,
    statement: str,
    *,
    supported: bool,
    statistic: float | None,
    description: str,
    call_id: str | None,
) -> Hypothesis:
    """One tested hypothesis.

    ``supported`` always comes from a test over tool output, never from a
    model. The model may only supply ``statement`` -- the words -- and even
    those have had their numbers stripped.
    """
    return Hypothesis(
        id=hypothesis_id,
        template=statement,
        tested_by=call_id or "",
        supported=supported,
        test_statistic=(
            Range(min=statistic, max=statistic, unit=Unit.FRACTION)
            if statistic is not None
            else None
        ),
        test_description=description,
        evidence=[call_id] if call_id else [],
    )


def _build_causal(result: ExecutionResult, intent: Intent, turn_id: str) -> Recommendation:
    """Why productivity has changed here.

    **The hypothesis discipline from CLAUDE.md is the whole point of this
    builder:** the LLM proposes hypotheses, deterministic code tests them, and
    an untested hypothesis is DROPPED rather than reported. So every
    ``Hypothesis`` emitted here carries ``tested_by`` naming the tool call that
    tested it and ``supported`` set from the number that came back -- never
    from plausibility.

    There is no verdict, because "why has the catch fallen" is not a go/no-go
    question and inventing one would be a safety claim nobody asked for.
    """
    chl = _first_output(result, "chl_anomaly")
    if chl is None or chl.status is ToolStatus.FAILED:
        return _no_data_answer(
            QueryType.CAUSAL_EXPLAIN, turn_id, result,
            "Could not test any explanation - the chlorophyll data did not return.",
        )
    chl_id = _first_call_id(result, "chl_anomaly")

    claims = [
        Claim(
            id="chl_now",
            kind=ClaimKind.OBSERVED,
            template="Chlorophyll is {min_mg} to {max_mg} mg/m3.",
            slots={"min_mg": chl.concentration.min, "max_mg": chl.concentration.max},
            evidence=[chl_id] if chl_id else [],
        )
    ]

    hypotheses: list[Hypothesis] = []
    findings: list[NegativeFinding] = []

    if chl.anomaly_sigma is None:
        # Untested, so not reported as a hypothesis at all. The absence of a
        # baseline is stated instead of being papered over with a bare
        # concentration the reader would inevitably interpret as normal or not.
        findings.append(
            NegativeFinding(
                id="no_baseline",
                template=(
                    "No monthly baseline exists for this location, so it cannot "
                    "be said whether chlorophyll is unusual."
                ),
                checked_by=chl_id,
            )
        )
    else:
        supported = abs(chl.anomaly_sigma) >= _ANOMALY_SUPPORTED_SIGMA
        direction = "below" if chl.anomaly_sigma < 0 else "above"
        claims.append(
            Claim(
                id="chl_baseline",
                kind=ClaimKind.DERIVED,
                # The SIGNED sigma, not its magnitude.
                #
                # This read abs(chl.anomaly_sigma) and paired it with a
                # "below"/"above" word, which is more natural English and is a
                # number the tool never produced. The verifier caught it:
                # "asserts 0.27 but no cited call returned a value that rounds
                # to it". Transforming a figure after it leaves the tool is
                # precisely what the verifier exists to stop, and it does not
                # care that the transformation was ours rather than a model's.
                template=(
                    "The {month} average here is {mean} mg/m3, and today is "
                    "{sigma} standard deviations from it ({direction} normal)."
                ),
                slots={
                    "month": chl.month,
                    "mean": chl.climatology_mean,
                    "sigma": chl.anomaly_sigma,
                    "direction": direction,
                },
                evidence=[chl_id] if chl_id else [],
                derived_by=chl_id,
            )
        )
        hypotheses.append(
            _hypothesis(
                "low_productivity",
                "Primary productivity is unusually low, reducing the food "
                "supply that supports the fishery.",
                supported=bool(supported and chl.anomaly_sigma < 0),
                statistic=chl.anomaly_sigma,
                description=(
                    f"Chlorophyll anomaly against a per-pixel {chl.month:02d} "
                    f"baseline; |sigma| >= {_ANOMALY_SUPPORTED_SIGMA} counts as support."
                ),
                call_id=chl_id,
            )
        )

    front = _first_output(result, "thermal_front")
    if front is not None and front.status is not ToolStatus.FAILED:
        hypotheses.append(
            _hypothesis(
                "weak_frontal_structure",
                "Weak frontal structure is giving fish less reason to "
                "aggregate in this area.",
                supported=not front.fronts,
                statistic=front.max_gradient_deg_c_per_km,
                description=(
                    "Count of sea-surface temperature fronts above the detection "
                    "threshold in the study box; zero counts as support."
                ),
                call_id=_first_call_id(result, "thermal_front"),
            )
        )

    # -- the LLM widens the search ----------------------------------------
    #
    # CLAUDE.md: the LLM proposes hypotheses, code tests them, and untested
    # ones are DROPPED. The two above are the floor -- what the answer falls
    # back to when no model is reachable. Everything the model adds has already
    # been through a real test in agents/hypotheses.py, and a proposal naming a
    # test we do not have never arrives here at all.
    proposed, proposal_error = propose_hypotheses(
        result, intent.spatial_reference.name if intent.spatial_reference else None,
        # Explainer wording only (number-stripped in rag/store.py, [] when the
        # store is unconfigured). The TESTS gate inside propose() is unchanged:
        # background can suggest an angle, never an outcome.
        background=background_for_causal(
            intent.spatial_reference.name if intent.spatial_reference else None
        ),
    )
    known = {h.id for h in hypotheses}
    for proposal in proposed:
        if proposal.test_id in known:
            continue
        known.add(proposal.test_id)
        hypotheses.append(
            _hypothesis(
                proposal.test_id,
                proposal.statement,
                supported=proposal.outcome.supported,
                statistic=proposal.outcome.statistic,
                description=proposal.outcome.description,
                call_id=proposal.tool_call_id,
            )
        )

    supported_count = sum(1 for h in hypotheses if h.supported)
    if supported_count:
        headline = Templated(
            template="{n} of {total} tested explanations are supported by the data.",
            slots={"n": supported_count, "total": len(hypotheses)},
        )
    else:
        headline = Templated(
            template=(
                "No tested explanation is supported: conditions here are within "
                "their normal range."
            )
        )

    caveats = _place_caveat(result) + [
        Caveat(
            text=(
                "Only hypotheses that could be tested against data are shown. "
                "Untested explanations are dropped rather than listed."
            )
        ),
        Caveat(
            text=(
                "Ocean productivity is one driver among many. Fishing effort, "
                "gear and market factors are not measured here."
            )
        ),
    ]

    return Recommendation(
        query_type=QueryType.CAUSAL_EXPLAIN,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        headline=headline,
        claims=claims,
        hypotheses=hypotheses,
        negative_findings=findings,
        spatial_context=_spatial_from_place(result, intent),
        confidence=Confidence(
            overall=0.25 if chl.anomaly_sigma is None else 0.55,
            by_claim={c.id: 0.7 for c in claims},
            basis=(
                f"{chl.provenance.source}; {chl.quality.describe()}"
                if chl.quality
                else chl.provenance.source
            ),
        ),
        caveats=caveats,
        evidence=_evidence_entries(result),
        visual_layers=[
            VisualLayer(id="chl_anomaly", type="raster", ref=chl_id)
        ] if chl_id else [],
        reasoning_trace=result.trace,
        degraded=result.degraded,
        degradation_notes=result.degradation_notes,
    )


def _build_conditions(result: ExecutionResult, intent: Intent, turn_id: str) -> Recommendation:
    """Tide, weather and alert status for a place. A report, not advice.

    No verdict, no drivers, no window, no guidance: every one of those would
    be adjudication, and this query type exists precisely for questions asked
    without a vessel class. Waves and wind are OBSERVED claims (verifier must
    match them); tide and alerts arrive through _negative_findings, the same
    helper the safety path uses, which is why the fallback plan keeps the
    safety step numbering (s4 tides, s5 alerts).
    """
    wave = _first_output(result, "wave_forecast")
    if wave is None:
        return _no_data_answer(
            QueryType.CONDITIONS_REPORT, turn_id, result,
            "Could not read conditions - the wave forecast did not return.",
        )
    wave_id = _first_call_id(result, "wave_forecast")
    wind_id = _first_call_id(result, "wind_forecast")

    claims: list[Claim] = [
        Claim(
            id="c1",
            kind=ClaimKind.OBSERVED,
            template="Waves {min}-{max} {unit}.",
            slots={
                "min": round(wave.significant_wave_height.min, 2),
                "max": round(wave.significant_wave_height.max, 2),
                "unit": "m",
            },
            evidence=[wave_id] if wave_id else [],
        )
    ]
    wind = _first_output(result, "wind_forecast")
    if wind is not None and wind_id:
        claims.append(
            Claim(
                id="c2",
                kind=ClaimKind.OBSERVED,
                template="Wind {min}-{max} {unit}.",
                slots={
                    "min": round(wind.wind_speed.min, 2),
                    "max": round(wind.wind_speed.max, 2),
                    "unit": "kn",
                },
                evidence=[wind_id],
            )
        )

    place_name = intent.spatial_reference.name if intent.spatial_reference else "the coast"
    caveats = _place_caveat(result) + [
        Caveat(
            text=(
                "This is a conditions report, not a safety assessment. It "
                "carries no verdict; ask whether it is safe to go out, naming "
                "your boat, before departing."
            )
        )
    ]

    quality_notes = [
        f"{e.tool} {e.source}"
        + (f", {e.data_age_days:.1f} days old" if e.data_age_days else "")
        for e in _evidence_entries(result)
        if e.status == "ok"
    ]

    return Recommendation(
        query_type=QueryType.CONDITIONS_REPORT,
        turn_id=turn_id,
        generated_at=datetime.now(IST),
        # No verdict. See the note at the top of this builder.
        headline=Templated(template=f"Conditions near {place_name}.", slots={"place": place_name}),
        claims=claims,
        negative_findings=_negative_findings(result),
        spatial_context=_spatial_from_place(result, intent),
        confidence=Confidence(
            overall=0.3 if result.degraded else 0.6,
            by_claim={c.id: 0.6 for c in claims},
            basis="; ".join(quality_notes) or "No successful data source.",
        ),
        caveats=caveats,
        evidence=_evidence_entries(result),
        # Filtered, like every other builder -- a bare None here is a
        # ValidationError that takes the whole answer with it.
        #
        # This is why conditions reports failed intermittently on 2026-09-07
        # with "the data came back but the answer could not be assembled".
        # `call_id_for("s1")` finds a step *named* s1, which the hardcoded
        # fallback plan always has and a model-written plan need not: the
        # model numbers its own steps. So the same question succeeded when the
        # fallback ran and failed when the planner's own plan did -- the
        # failures in that session were exactly the turns not marked
        # "fallback plan".
        visual_layers=[
            layer
            for layer in (
                VisualLayer(id="origin", type="point", ref=result.call_id_for("s1"))
                if result.call_id_for("s1")
                else None,
                VisualLayer(id="wave_24h", type="timeseries", ref=wave_id)
                if wave_id
                else None,
            )
            if layer is not None
        ],
        reasoning_trace=result.trace,
        degraded=result.degraded,
        degradation_notes=result.degradation_notes,
    )


def _first_output(result: ExecutionResult, tool_name: str):
    """The output of the first step that ran ``tool_name``, or None.

    Looked up by tool rather than by step id, because step numbering differs
    between a hardcoded fallback plan, a composed plan and whatever the model
    emitted. Hardcoding "s3" here would work until the day a plan had one more
    geospatial step, and then it would silently read the wrong tool's output.
    """
    for record in result.tool_call_log.values():
        if record.tool == tool_name and record.step_id:
            output = result.outputs.get(record.step_id)
            if output is not None:
                return output
    return None


def _first_call_id(result: ExecutionResult, tool_name: str) -> str | None:
    for call_id, record in result.tool_call_log.items():
        if record.tool == tool_name:
            return call_id
    return None
