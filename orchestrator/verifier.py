"""The verifier. Deterministic, not an LLM.

Walks every claim in a recommendation and confirms each number appears in the
referenced tool output. Rejects for revision if not. This is ORCA's headline
technical claim, so it has to actually work rather than merely exist.

---------------------------------------------------------------------------
WHAT IT CHECKS AGAINST, AND WHY THAT CHOICE MATTERS
---------------------------------------------------------------------------

The verifier checks against an **independent tool call log**, never against the
recommendation's own ``evidence[]`` block.

This is the single most important decision in the module. ``evidence[]`` is
assembled by the synthesis agent -- the same layer that writes the claims. If
the verifier trusted it, an agent that invented a wave height could invent a
matching evidence digest to go with it, and the check would pass while being
completely hollow. Verifying a document against itself proves nothing.

So ``verify()`` takes the log written by the executor at call time, and it also
checks ``evidence[]`` *against* that log. A fabricated evidence entry is itself
a violation.

---------------------------------------------------------------------------
THE MATCHING RULE
---------------------------------------------------------------------------

Claims carry display-rounded numbers: a tool returns 2.18 and the claim says
2.2. A naive equality check would reject every honest answer, and a loose
tolerance would accept invented ones.

The rule used here is: **a claimed number is valid if it is the correct
rounding, at its own precision, of some number the tool actually returned.**
2.2 matches 2.18 because ``round(2.18, 1) == 2.2``. It does not match 2.7. The
claim's own precision sets the tolerance, so a claim cannot buy slack by being
vague -- writing 2 instead of 2.2 widens the window but also says less.

Strictness by claim kind, as required by CLAUDE.md:

* ``OBSERVED`` -- must match. Failure is an ERROR and rejects the answer.
* ``DERIVED``  -- must match. Also an ERROR: a derived number is computed from
  tool outputs by a named rule, so if it traces to nothing, the rule did not
  run and something else produced that figure.
* ``INFERRED`` -- a mismatch is a WARNING, not a rejection. Inferred claims are
  the LLM's own hypotheses and may legitimately be vague. They may not be
  *numerically* vague -- the schema already refuses an unevidenced number on an
  inferred claim -- but a soft number here does not poison a safety answer the
  way a wrong wave height does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from core.schemas.recommendation import ClaimKind, Recommendation
from core.schemas.tool_io import ToolCallRecord, ToolStatus

__all__ = [
    "Severity",
    "Violation",
    "VerificationReport",
    "verify",
    "numbers_match",
]


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Violation:
    """One thing that did not check out."""

    severity: Severity
    code: str
    where: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.severity.value}] {self.code} at {self.where}: {self.detail}"


@dataclass
class VerificationReport:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    numbers_checked: int = 0
    claims_checked: int = 0

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARNING]

    def revision_feedback(self) -> str:
        """Message handed back to synthesis when the answer is rejected."""
        lines = [
            "This answer was rejected by the verifier. Every number in a claim "
            "must appear in the tool output it cites."
        ]
        lines += [f"  - {v.where}: {v.detail}" for v in self.errors]
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Number matching
# --------------------------------------------------------------------------


def _decimals(value: float | int) -> int:
    """How many decimal places a claimed value was written to.

    This is what sets the tolerance. An int is zero decimals; 2.20 and 2.2 are
    both one decimal, because the trailing zero is gone by the time Python
    holds the value.

    **Known limitation:** for the same reason, the float ``2.0`` is
    indistinguishable from the int ``2`` and is therefore checked at
    whole-number tolerance rather than at one decimal place. This only ever
    makes the verifier more permissive, never less. Fixing it would mean
    carrying claim values as strings or Decimals through the whole schema,
    which is a real cost for a case that does not arise in generated output.
    Pinned by ``test_trailing_zeros_cannot_tighten_the_window``.
    """
    if isinstance(value, int):
        return 0
    text = repr(float(value))
    if "e" in text or "E" in text:
        return 0
    _, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    return len(frac)


def numbers_match(claimed: float, actual: float) -> bool:
    """True if ``claimed`` is a correct rounding of ``actual`` at its precision."""
    places = _decimals(claimed)
    return round(actual, places) == round(float(claimed), places)


def _find(claimed: float, haystack: Iterable[float]) -> bool:
    return any(numbers_match(claimed, v) for v in haystack)


def _microdegrees(value: float) -> int:
    """Integer microdegrees. Exact comparison at the tool's own precision.

    The route tool emits waypoints rounded to 4dp, so scaling by 1e6 and
    rounding to an int compares exactly -- unlike numbers_match, which
    deliberately tolerates display rounding, and unlike round(x, 4) == y,
    which still compares binary floats. Used only for the route check."""
    return round(float(value) * 1_000_000)


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


def verify(
    rec: Recommendation,
    log: dict[str, ToolCallRecord],
    *,
    strict_evidence: bool = True,
) -> VerificationReport:
    """Verify a recommendation against the executor's tool call log.

    ``log`` maps ``tool_call_id`` to the record written when the call ran. It
    is the source of truth; ``rec.evidence`` is treated as a claim about that
    truth and is checked too.
    """
    violations: list[Violation] = []
    numbers_checked = 0

    def add(sev: Severity, code: str, where: str, detail: str) -> None:
        violations.append(Violation(sev, code, where, detail))

    def numbers_for(refs: Iterable[str]) -> list[float]:
        out: list[float] = []
        for ref in refs:
            record = log.get(ref)
            if record is not None:
                out.extend(record.output_numbers)
        return out

    # -- 1. every cited call must exist in the log, and must have succeeded --
    cited: set[str] = set()
    for c in rec.claims:
        cited.update(c.evidence)
    for d in rec.drivers:
        cited.update(d.evidence)
    for h in rec.hypotheses:
        cited.update(h.evidence)
        cited.add(h.tested_by)
    for a in rec.alternatives:
        cited.update(a.evidence)
    for nf in rec.negative_findings:
        cited.add(nf.checked_by)
    if rec.verdict is not None:
        cited.add(rec.verdict.computed_by)
    if rec.window is not None:
        cited.add(rec.window.basis)
    if rec.route is not None:
        cited.add(rec.route.computed_by)

    for ref in sorted(cited):
        if ref not in log:
            add(
                Severity.ERROR,
                "missing_tool_call",
                ref,
                "cited as evidence but no such call appears in the tool log. "
                "An answer may only cite calls that actually ran.",
            )

    # -- 2. evidence[] must not overstate what the log holds ---------------
    if strict_evidence:
        for entry in rec.evidence:
            record = log.get(entry.tool_call_id)
            if record is None:
                add(
                    Severity.ERROR,
                    "fabricated_evidence",
                    f"evidence[{entry.tool_call_id}]",
                    "evidence entry references a call that never ran.",
                )
                continue
            if entry.tool != record.tool:
                add(
                    Severity.ERROR,
                    "evidence_tool_mismatch",
                    f"evidence[{entry.tool_call_id}]",
                    f"claims tool {entry.tool!r} but the log says {record.tool!r}.",
                )
            for value in entry.numeric_values():
                numbers_checked += 1
                if not _find(value, record.output_numbers):
                    add(
                        Severity.ERROR,
                        "fabricated_evidence_value",
                        f"evidence[{entry.tool_call_id}]",
                        f"digest contains {value}, which the tool never returned. "
                        "The evidence block is not allowed to invent support.",
                    )

    # -- 3. negative findings must rest on calls that succeeded ------------
    for nf in rec.negative_findings:
        record = log.get(nf.checked_by)
        if record is not None and record.status is ToolStatus.FAILED:
            add(
                Severity.ERROR,
                "negative_finding_from_failed_check",
                f"negative_finding[{nf.id}]",
                f"asserts an absence, but {nf.checked_by} failed "
                f"({record.error!r}). Not knowing is not the same as nothing "
                "being there.",
            )

    # -- 3b. a route must rest on a call that succeeded -------------------
    if rec.route is not None:
        record = log.get(rec.route.computed_by)
        if record is None or record.status is ToolStatus.FAILED:
            add(
                Severity.ERROR,
                "route_from_failed_call",
                "route.computed_by",
                f"cites {rec.route.computed_by}, which "
                + (
                    "never ran. A route may only be drawn from a call that ran."
                    if record is None
                    else f"failed ({record.error!r}). A route from a failed "
                    "optimisation is not a route."
                ),
            )

    # -- 4. claims: the core walk ------------------------------------------
    for claim in rec.claims:
        failed = [r for r in claim.evidence if (log.get(r) or None) and log[r].status is ToolStatus.FAILED]
        if failed:
            add(
                Severity.ERROR,
                "claim_cites_failed_call",
                f"claim[{claim.id}]",
                f"cites {failed}, which failed.",
            )

        haystack = numbers_for(claim.evidence)
        severity = (
            Severity.WARNING if claim.kind is ClaimKind.INFERRED else Severity.ERROR
        )
        for value in claim.slot_numbers():
            numbers_checked += 1
            if not _find(value, haystack):
                add(
                    severity,
                    "unsupported_number",
                    f"claim[{claim.id}]",
                    f"asserts {value} ({claim.kind.value}) but no cited call "
                    f"({claim.evidence or 'none'}) returned a value that rounds "
                    f"to it.",
                )

    # -- 5. drivers: the observed side must be real ------------------------
    for driver in rec.drivers:
        haystack = numbers_for(driver.evidence)
        observed = driver.evaluation.observed
        candidates = [observed.min, observed.max]
        if observed.peak is not None:
            candidates.append(observed.peak)
        for value in candidates:
            numbers_checked += 1
            if not _find(value, haystack):
                add(
                    Severity.ERROR,
                    "unsupported_driver_value",
                    f"driver[{driver.id}]",
                    f"observed value {value} {observed.unit.value} does not "
                    f"appear in {driver.evidence}.",
                )
        # The threshold is not checked here: it comes from
        # config/risk_thresholds.yaml, not from a tool. Its citation pointer is
        # checked by tests/test_thresholds.py instead.

    # -- 6. the verdict: the number that matters most ----------------------
    if rec.verdict is not None:
        record = log.get(rec.verdict.computed_by)
        if record is not None:
            if record.tool != "compute_risk_score":
                add(
                    Severity.ERROR,
                    "verdict_from_wrong_tool",
                    "verdict.computed_by",
                    f"points at {record.tool!r}. A safety verdict may only come "
                    "from compute_risk_score().",
                )
            numbers_checked += 1
            if not _find(rec.verdict.score, record.output_numbers):
                add(
                    Severity.ERROR,
                    "unsupported_verdict_score",
                    "verdict.score",
                    f"score {rec.verdict.score} does not appear in the output of "
                    f"{rec.verdict.computed_by}. This is the number the whole "
                    "governing rule exists to protect.",
                )

    # -- 7. a resolved coordinate must have been resolved by a tool --------
    ctx = rec.spatial_context
    if ctx is not None and ctx.origin is not None:
        record = log.get(ctx.origin.resolved_by)
        if record is not None:
            for value in (ctx.origin.lat, ctx.origin.lon):
                numbers_checked += 1
                if not _find(value, record.output_numbers):
                    add(
                        Severity.ERROR,
                        "invented_coordinate",
                        "spatial_context.origin",
                        f"coordinate component {value} was not returned by "
                        f"{ctx.origin.resolved_by}. Coordinates come from "
                        "resolve_place(), never from the model.",
                    )

    # -- 8. alternatives ---------------------------------------------------
    for alt in rec.alternatives:
        haystack = numbers_for(alt.evidence)
        for value in alt.slot_numbers():
            numbers_checked += 1
            if not _find(value, haystack):
                add(
                    Severity.ERROR,
                    "unsupported_number",
                    f"alternative[{alt.id}]",
                    f"asserts {value} but no cited call returned it.",
                )

    # -- 9. hypotheses -----------------------------------------------------
    for hyp in rec.hypotheses:
        if hyp.tested_by not in log:
            continue
        if log[hyp.tested_by].status is ToolStatus.FAILED:
            add(
                Severity.ERROR,
                "untested_hypothesis",
                f"hypothesis[{hyp.id}]",
                "its test failed, so the hypothesis is untested and must be "
                "dropped rather than reported.",
            )

    # -- 10. the route: structured waypoint comparison, not the haystack -
    #
    # Waypoints are NOT checked with numbers_match/_find. The flat haystack
    # (provenance.flatten_numbers, via ToolCallLog.record) mixes waypoint
    # latitudes with distance_km/estimated_hours, so a flat _find could match
    # a claimed latitude against a distance that happens to share its digits.
    # Structured pairwise comparison against the cited call's output list
    # removes that cross-match class entirely: same count, same order, exact
    # equality at integer microdegrees (the tool already emits 4dp, so 1e-6
    # integer comparison is exact, with no float-repr tolerance window).
    #
    # Residual risk, stated honestly: this proves *provenance* -- the drawn
    # points are the tool's points -- not geodetic truth. The tool's own
    # rounding is trusted because the tool is the authority here.
    if rec.route is not None:
        record = log.get(rec.route.computed_by)
        if record is not None and record.status is not ToolStatus.FAILED:
            for value, name in (
                (rec.route.distance_km, "route.distance_km"),
                (rec.route.estimated_hours, "route.estimated_hours"),
            ):
                numbers_checked += 1
                if not _find(value, record.output_numbers):
                    add(
                        Severity.ERROR,
                        "unsupported_number",
                        name,
                        f"asserts {value} but {rec.route.computed_by} returned no "
                        "value that rounds to it.",
                    )
            raw = (record.output or {}).get("waypoints")
            claimed = rec.route.waypoints
            if not isinstance(raw, list) or len(raw) != len(claimed):
                numbers_checked += 1
                got = len(raw) if isinstance(raw, list) else "non-list"
                add(
                    Severity.ERROR,
                    "route_waypoint_mismatch",
                    "route.waypoints",
                    f"route carries {len(claimed)} waypoints but the cited call "
                    f"{rec.route.computed_by} holds {got}. Count must match "
                    "exactly; a drawn point with no tool point is invented.",
                )
            else:
                for index, (point, ref) in enumerate(zip(claimed, raw)):
                    numbers_checked += 2
                    ref_lat = ref.get("lat") if isinstance(ref, dict) else None
                    ref_lon = ref.get("lon") if isinstance(ref, dict) else None
                    if (
                        not isinstance(ref_lat, (int, float))
                        or not isinstance(ref_lon, (int, float))
                        or isinstance(ref_lat, bool)
                        or isinstance(ref_lon, bool)
                        or _microdegrees(point.lat) != _microdegrees(ref_lat)
                        or _microdegrees(point.lon) != _microdegrees(ref_lon)
                    ):
                        add(
                            Severity.ERROR,
                            "route_waypoint_mismatch",
                            f"route.waypoints[{index}]",
                            f"drawn as ({point.lat}, {point.lon}) but the cited "
                            f"call holds ({ref_lat}, {ref_lon}). Coordinates come "
                            "from the tool output verbatim, never from the model.",
                        )

    errors = [v for v in violations if v.severity is Severity.ERROR]
    return VerificationReport(
        ok=not errors,
        violations=violations,
        numbers_checked=numbers_checked,
        claims_checked=len(rec.claims),
    )
