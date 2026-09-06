"""Frozen schema package. Imports from nothing; everything imports from it.

Import order inside this package is strictly one-directional::

    core.units  ->  intent  ->  {plan, recommendation, tool_io}

Nothing here may import from ``tools``, ``agents``, ``orchestrator`` or
``ingest``. If a schema needs to know about a tool, the dependency is pointing
the wrong way -- the tool should carry the schema instead.

Frontend TypeScript types are generated from these models. Do not hand-write
them on the other side; they will drift within a week.
"""

from core.schemas.intent import (
    SAFETY_CRITICAL_SLOTS,
    ClarificationRequest,
    Intent,
    PlannerOutput,
    QueryType,
    Refusal,
    RefusalReason,
    SpatialReference,
    TimeWindow,
    VesselClass,
)
from core.schemas.plan import (
    MAX_PLAN_STEPS,
    Plan,
    PlanStep,
    extract_references,
    parse_reference,
)
from core.schemas.recommendation import (
    SCHEMA_VERSION,
    Alternative,
    AlternativeCost,
    Assumption,
    Caveat,
    Claim,
    ClaimKind,
    Confidence,
    Driver,
    EvidenceEntry,
    Hypothesis,
    NegativeFinding,
    OperationalGuidance,
    ReasoningStep,
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
from core.schemas.tool_io import (
    DataQuality,
    GeoPoint,
    Provenance,
    ToolCallRecord,
    ToolInput,
    ToolOutput,
    ToolStatus,
)
from core.units import (
    Comparison,
    Range,
    Threshold,
    ThresholdEvaluation,
    Unit,
    evaluate,
    knots_to_ms,
    ms_to_knots,
)

__all__ = [
    "SCHEMA_VERSION",
    "MAX_PLAN_STEPS",
    "SAFETY_CRITICAL_SLOTS",
    # units
    "Unit",
    "Range",
    "Threshold",
    "ThresholdEvaluation",
    "Comparison",
    "evaluate",
    "ms_to_knots",
    "knots_to_ms",
    # intent
    "QueryType",
    "VesselClass",
    "Intent",
    "SpatialReference",
    "TimeWindow",
    "ClarificationRequest",
    "Refusal",
    "RefusalReason",
    "PlannerOutput",
    # plan
    "Plan",
    "PlanStep",
    "parse_reference",
    "extract_references",
    # recommendation
    "Recommendation",
    "Templated",
    "Claim",
    "ClaimKind",
    "Verdict",
    "VerdictValue",
    "Driver",
    "Trajectory",
    "Window",
    "TurnBackBasis",
    "NegativeFinding",
    "Hypothesis",
    "OperationalGuidance",
    "Alternative",
    "AlternativeCost",
    "SpatialContext",
    "ResolvedPlace",
    "Assumption",
    "Confidence",
    "Caveat",
    "EvidenceEntry",
    "VisualLayer",
    "ReasoningStep",
    # tool io
    "ToolInput",
    "ToolOutput",
    "ToolStatus",
    "ToolCallRecord",
    "Provenance",
    "DataQuality",
    "GeoPoint",
]
