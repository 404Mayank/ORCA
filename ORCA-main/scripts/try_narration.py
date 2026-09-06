"""Exercise the live narration path. Needs a real key in .env.

    python scripts/try_narration.py

Runs the full safety_assess pipeline on cached data, verifies the object,
then narrates it through the configured LLM and reports whether the prose
passed the number guard. Not a test -- it spends money and needs a network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Models emit Unicode punctuation (non-breaking hyphens, en dashes) and the
# Windows console defaults to cp1252, which cannot print them. Found on the
# very first live run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import core.env  # noqa: F401 -- loads .env
from agents.narrate import narrate
from agents.synthesis_agent import build_recommendation
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from orchestrator.executor import execute_plan_sync
from orchestrator.llm.client import provider_status
from orchestrator.validate_plan import fallback_plan, validate_plan
from orchestrator.verifier import verify


def main() -> int:
    print("providers:", provider_status())

    plan = validate_plan(
        fallback_plan(QueryType.SAFETY_ASSESS, {"PLACE": "Nagapattinam", "VESSEL_CLASS": "frp_9m"})
    ).plan
    result = execute_plan_sync(plan, turn_id="t_live")
    intent = Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to go out tomorrow morning?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    )
    rec = build_recommendation(result, intent, turn_id="t_live")
    report = verify(rec, result.tool_call_log)
    print(f"verifier: ok={report.ok} numbers_checked={report.numbers_checked}")
    if not report.ok:
        for v in report.errors:
            print("  ", v)
        return 1

    narration = narrate(rec)
    print(f"\nnarration source: {narration.source}  model: {narration.model or '-'}")
    if narration.fallback_reason:
        print(f"fallback reason : {narration.fallback_reason}")
    print("-" * 72)
    print(narration.text)
    print("-" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
