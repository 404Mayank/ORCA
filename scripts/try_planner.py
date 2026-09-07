"""Exercise the live planner, then run whatever it planned. Needs a key.

    python scripts/try_planner.py "is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"

Runs: plan -> validate -> execute -> synthesise -> verify -> narrate.
The first time the whole system answers a question it has never seen before.
Not a test -- it spends money and needs a network.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import core.env  # noqa: F401
from agents.intent_planner_agent import plan_query
from agents.narrate import narrate
from agents.synthesis_agent import build_recommendation
from core.schemas.intent import QueryType
from orchestrator.executor import execute_plan_sync
from orchestrator.llm.client import provider_status
from orchestrator.verifier import verify

DEFAULT = "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"


def main(query: str) -> int:
    print("providers:", provider_status())
    print("query    :", query)

    planned = plan_query(query)
    print(f"\nplanner  : state={planned.state} attempts={planned.attempts} "
          f"fallback={planned.used_fallback} via {planned.llm_provider}/{planned.llm_model or '-'}")
    for note in planned.notes:
        print("  note:", note)

    intent = planned.output.intent
    print(f"intent   : {intent.query_type.value} place={intent.spatial_reference.name if intent.spatial_reference else None} "
          f"vessel={intent.vessel_class.value if intent.vessel_class else None} inherited={intent.inherited_slots}")

    if planned.state == "clarification":
        c = planned.output.clarification
        print(f"\nCLARIFICATION: {c.question_template}  options={c.options}")
        return 0
    if planned.state == "refusal":
        print(f"\nREFUSAL: {planned.output.refusal.reason.value} - {planned.output.refusal.explanation_template}")
        return 0

    print("\nplan:")
    for step in planned.plan.steps:
        print(f"  {step.id:3s} {step.tool:22s} depends_on={step.depends_on}")

    result = execute_plan_sync(planned.plan, turn_id="t_live")
    print("\nexecution:", "degraded" if result.degraded else "clean")
    for t in result.trace:
        print(f"  {t.step:3s} {t.tool:22s} {t.status:8s} {t.ms if t.ms is not None else '-':>5} ms")

    if intent.query_type is not QueryType.SAFETY_ASSESS:
        print("\n(synthesis for this query type is not built yet; stopping after execution)")
        return 0

    rec = build_recommendation(result, intent, turn_id="t_live")
    report = verify(rec, result.tool_call_log)
    print(f"\nverifier : ok={report.ok} numbers_checked={report.numbers_checked}")
    for v in report.errors:
        print("  ", v)

    narration = narrate(rec)
    print(f"narration: {narration.source} {narration.model or ''}"
          + (f"  (fallback: {narration.fallback_reason})" if narration.fallback_reason else ""))
    print("-" * 72)
    print(narration.text)
    print("-" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(" ".join(sys.argv[1:]) or DEFAULT))
