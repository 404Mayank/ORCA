"""Base types every tool's input and output inherit from.

The tool layer is where all real computation lives. These bases exist so that
three things are true of *every* tool without each tool having to remember:

1.  Its output carries provenance -- which dataset, retrieved when, at what
    native resolution, how stale.
2.  Its output records data quality honestly, so a 7-day composite cannot
    quietly pass as this morning's pass.
3.  Its numbers are discoverable by the verifier through one interface,
    :meth:`ToolOutput.numeric_values`, rather than the verifier knowing the
    shape of twenty different return types.

``tools/registry.py`` maps a tool name to its function plus its input and
output models, and injects that mapping into the planner prompt
programmatically. Nothing here imports the registry; the dependency runs the
other way.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DataQuality",
    "Provenance",
    "ToolInput",
    "ToolOutput",
    "ToolStatus",
    "ToolCallRecord",
    "GeoPoint",
]


class GeoPoint(BaseModel):
    """A position. Used pervasively enough to belong in the base module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class DataQuality(BaseModel):
    """How good the underlying data actually was.

    Cloud gaps are the single biggest data risk in this project -- monsoon
    Coromandel is overcast for days at a time and an L2 satellite product will
    be full of holes during the demo. This block travels with every gridded
    value so that degradation is visible rather than inferred.

    ``confidence.basis`` in the recommendation is assembled from these fields.
    That is the mechanism by which "7-day composite, last clear pass 3 days
    ago" reaches the user instead of a bare number that looks fresh.
    """

    model_config = ConfigDict(extra="forbid")

    data_age_days: float | None = Field(
        default=None, ge=0.0, description="Age of the underlying observation."
    )
    clear_pass_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fraction of the AOI with a usable satellite pass.",
    )
    composite_window_days: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Set when the value came from a 3-day or 7-day composite rather "
            "than a single pass. The fallback ladder is single -> 3-day -> "
            "7-day, and which rung was used is never hidden."
        ),
    )
    gap_filled: bool = Field(
        default=False, description="True for L4 products with interpolated cells."
    )
    coverage_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Valid cells over requested cells."
    )
    notes: list[str] = Field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        """Whether this value should be flagged rather than presented plainly."""
        return (
            (self.data_age_days is not None and self.data_age_days > 1.0)
            or (self.composite_window_days is not None and self.composite_window_days > 1)
            or (self.clear_pass_fraction is not None and self.clear_pass_fraction < 0.5)
            or (self.coverage_fraction is not None and self.coverage_fraction < 0.9)
        )

    def describe(self) -> str:
        """One line for ``confidence.basis``. Plain, not reassuring."""
        parts: list[str] = []
        if self.composite_window_days and self.composite_window_days > 1:
            parts.append(f"{self.composite_window_days}-day composite")
        if self.data_age_days is not None:
            parts.append(f"last clear pass {self.data_age_days:.0f} days ago")
        if self.gap_filled:
            parts.append("gap-filled (L4)")
        if self.clear_pass_fraction is not None:
            parts.append(f"{self.clear_pass_fraction:.0%} clear")
        return ", ".join(parts) if parts else "single pass, current"


class Provenance(BaseModel):
    """Where a value came from. Recorded on every external fetch.

    Required rather than optional, because a value with no provenance cannot
    be defended and should not have been ingested. ``query`` holds the exact
    request -- the ERDDAP URL, the Open-Meteo parameter string -- so a result
    can be reproduced months later when a judge asks where a number came from.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, description="Dataset id, or 'deterministic'.")
    source_url: str | None = None
    query: str | None = Field(default=None, description="The exact request issued.")
    retrieved_at: datetime | None = None
    issued_at: datetime | None = Field(
        default=None, description="Forecast issue time, distinct from retrieval time."
    )
    native_resolution_deg: float | None = None
    native_units: str | None = Field(
        default=None,
        description=(
            "Units as the source published them, before conversion. Half the "
            "failure modes in this project are unit confusion, and this field "
            "is what makes a bad conversion visible in a diff."
        ),
    )
    authority: str | None = Field(default=None, description="IMD, INCOIS, Copernicus.")


class ToolInput(BaseModel):
    """Base for every tool's argument model.

    ``extra="forbid"`` is the point: a planner that invents an argument gets a
    validation error from ``validate_plan()`` before anything executes, rather
    than a silently ignored keyword.
    """

    model_config = ConfigDict(extra="forbid")


class ToolStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    FAILED = "failed"


class ToolOutput(BaseModel):
    """Base for every tool's return model.

    Subclasses add their own typed fields. The verifier does not need to know
    about those fields; it calls :meth:`numeric_values` and gets every number
    the tool produced, which is the haystack a claim's numbers must be found in.
    """

    model_config = ConfigDict(extra="forbid")

    provenance: Provenance
    quality: DataQuality = Field(default_factory=DataQuality)
    status: ToolStatus = ToolStatus.OK
    error: str | None = None

    def numeric_values(self) -> list[float]:
        """Every number in the payload, flattened.

        Provenance and quality metadata are excluded -- a resolution of 0.05
        must not accidentally satisfy a claim about a 0.05 anything else. Only
        the tool's actual results count as evidence.
        """
        skip = {"provenance", "quality", "status", "error"}
        out: list[float] = []

        def walk(node: Any) -> None:
            if isinstance(node, bool):
                return
            if isinstance(node, (int, float)):
                out.append(float(node))
            elif isinstance(node, BaseModel):
                walk(node.model_dump())
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, (list, tuple, set)):
                for v in node:
                    walk(v)

        payload = {k: v for k, v in self.model_dump().items() if k not in skip}
        walk(payload)
        return out

    def digest(self) -> dict[str, Any]:
        """The compact form stored in ``EvidenceEntry.output_digest``.

        Full outputs can be large -- a 24-hour timeseries on an 81x71 grid is
        not something to keep per turn. The digest keeps the scalars a claim
        might quote and drops the bulk.
        """
        skip = {"provenance", "quality", "status", "error"}
        payload = self.model_dump()
        return {
            k: v
            for k, v in payload.items()
            if k not in skip and not isinstance(v, (list, tuple)) or k in ("bounds",)
        }


class ToolCallRecord(BaseModel):
    """One executed call, as written to the ``tool_calls`` table.

    This is the verifier's source of truth and the explainability drawer's
    backing store. It is persisted rather than held in memory so a
    recommendation can be re-verified later, which is what makes replay and
    evaluation possible.
    """

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    step_id: str | None = None
    turn_id: str | None = None

    args: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    output_numbers: list[float] = Field(
        default_factory=list,
        description=(
            "Pre-flattened numeric values from the output. Denormalised on "
            "purpose: the verifier runs this lookup for every number in every "
            "claim, and re-walking nested output each time is wasteful."
        ),
    )

    status: ToolStatus = ToolStatus.OK
    error: str | None = None
    started_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)

    def contains(self, value: float, tolerance: float = 1e-6) -> bool:
        """Whether a number appears in this call's output.

        Absolute tolerance by default, because the values being matched are
        physical quantities already rounded for display -- 2.8 m, 15 knots.
        The verifier owns the policy for how much rounding is acceptable per
        claim kind; this is just the lookup.
        """
        return any(abs(v - value) <= tolerance for v in self.output_numbers)
