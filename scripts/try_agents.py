"""Run every query type through the composed agent path, on live cached data.

    python scripts/refresh_cache.py     # first, to populate the cache
    python scripts/try_agents.py

Composes a plan from the domain agents, validates it, executes it, and prints
each agent's typed fragment. No LLM and no network: the planner is bypassed and
the intent is supplied directly, so what this exercises is the deterministic
half -- composition, execution, and each domain's degradation policy.

Not a test. It needs a populated cache, which ``tests/test_agents.py``
deliberately does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agents.base import compose_plan, fragments_for
from core.schemas.intent import Intent, QueryType, SpatialReference, VesselClass
from orchestrator.executor import execute_plan_sync
from orchestrator.validate_plan import validate_plan

CASES = [
    Intent(
        query_type=QueryType.SAFETY_ASSESS,
        raw_query="is it safe to take my FRP boat out from Nagapattinam tomorrow morning?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.FRP_9M,
    ),
    Intent(
        query_type=QueryType.PFZ_LOCATE,
        raw_query="where is the nearest fishing zone?",
        spatial_reference=SpatialReference(name="Nagapattinam"),
        vessel_class=VesselClass.MECHANISED_TRAWLER,
    ),
    Intent(
        query_type=QueryType.GEOFENCE_CHECK,
        raw_query="which zones must I avoid off Rameswaram?",
        spatial_reference=SpatialReference(name="Rameswaram"),
    ),
    Intent(
        query_type=QueryType.CAUSAL_EXPLAIN,
        raw_query="why has the catch fallen off Cuddalore?",
        spatial_reference=SpatialReference(name="Cuddalore"),
    ),
]


def _substitute(node, values):
    """Fill $PLACE and $VESSEL_CLASS, exactly as fallback_plan() does."""
    if isinstance(node, str) and node.startswith("$") and node[1:] in values:
        return values[node[1:]]
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def run(intent: Intent) -> bool:
    print("=" * 78)
    print(f"{intent.query_type.value}: {intent.raw_query}")

    raw = compose_plan(intent)
    if raw is None:
        print("  no agent contributed a step -- the intent is missing a slot")
        return False

    values = {"PLACE": intent.spatial_reference.name}
    if intent.vessel_class:
        values["VESSEL_CLASS"] = intent.vessel_class.value
    raw = _substitute(raw, values)

    print("  plan:", " -> ".join(f"{s['id']}:{s['tool']}" for s in raw["steps"]))

    validated = validate_plan(raw)
    if not validated.ok:
        print("  INVALID:")
        for error in validated.errors:
            print(f"    - {error}")
        return False

    result = execute_plan_sync(validated.plan, turn_id=f"t_{intent.query_type.value}")
    print(f"  ran {len(result.outputs)} steps, degraded={result.degraded}")

    for fragment in fragments_for(result, intent):
        flag = " BLOCKS VERDICT" if fragment.blocks_verdict else ""
        print(f"\n  [{fragment.agent}] {fragment.status.value}{flag}")
        for finding in fragment.findings:
            slots = {k: v for k, v in finding.slots.items() if v is not None}
            print(f"     {finding.key:22s} {json.dumps(slots, default=str)}")
        for note in fragment.notes:
            print(f"     note: {note}")
    return True


def main() -> int:
    ok = all([run(intent) for intent in CASES])
    print("=" * 78)
    print("All four query types ran." if ok else "Some query types did not run.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
