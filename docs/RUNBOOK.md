# Runbook: operate and demo ORCA

## Daily operation

| Task | Command |
|---|---|
| Start API | `.venv/bin/python -m uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000` |
| Start UI | `cd frontend && npm run dev` (uses 5174+ if 5173 held) |
| Health | `curl localhost:8000/health` (tools registered/implemented) |
| Readiness | `curl localhost:8000/readiness` (cache ages + provider flags) |
| Nightly ingest | `.venv/bin/python scripts/refresh_cache.py` (weather+ocean+alerts+static) |
| Pre-demo ingest | Same, plus check `reviewed_by`/`last_reviewed` in `config/active_alerts.yaml` (< 24 h) |

`readiness.llm_providers` reflects env keys: all false means template tier
(correct, stiff). `ORCA_TIER` is not exposed there; confirm it by turn latency
(~12 s paid, ~60–110 s free) or `/proc/<pid>/environ`.

## The demo that works (Fengal replay first, live second)

A calm-day live demo proves nothing — every verdict is `go`. Open on the
replay, then take a live question:

```bash
curl -X POST localhost:8000/replay/fengal  # no_go 71.5 h pre-landfall, via the UI Replay panel
.venv/bin/python scripts/replay.py         # CLI original (same code path, same table)
```

Then live: *"What are the weather and sea conditions near Nagapattinam?"*
(place-only, no vessel gate) and *"Is it safe to take my FRP boat out from
Nagapattinam tomorrow morning?"* (full verdict path). End on the evidence
drawer: every number, its `tool_call_id`.

## Model tiers (`ORCA_TIER`; live-switchable in Settings)

| Tier | Chain (2026-09-07) | When |
|---|---|---|
| `free` (default) | Zen Spark free pair → Go → groq | daily use, zero spend |
| `fast` | Go grok-4.6 first | interactive testing |
| `paid` | Go spark contributors → glm-5.3-flash → deepseek-v4-flash | demo day |

Zen has no credits on our key: paid Zen ids 401 (chain walks past them by
design). Contributor-free ids train on prompts (locations, no PII — accepted,
documented in `models.yaml`). Verify any new id live before configuring it:
roster presence ≠ servable (`deepseek-v4-flash-free` lists but 400s).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Turn spins for minutes | queued/starved model chain (free pool congested) | switch `ORCA_TIER=fast/paid`; check which chain id is serving with a pond probe |
| Suite takes minutes not seconds | live-model calls: `.env` auto-loads, scrub bypassed | `tests/conftest.py::hermetic_env` must cover the new var; never call real providers from tests |
| `pytest` collection errors on `tools.*` | geo extras missing in that interpreter | use `.venv/bin/python -m pytest`, install `[geo]` |
| Live-cache tests fail after a fresh clone | no `data/` (gitignored) | `scripts/refresh_cache.py --weather --alerts` (fast); `--ocean` for satellite |
| `test_generated_types_are_current` fails | schema changed without regen | `.venv/bin/python scripts/gen_types.py` + `cd frontend && npx tsc -b` |
| `pkill -f uvicorn...` kills your own shell | pattern matches your cmdline | kill by PID (`pgrep -f`), or bracket trick, and verify after |
| Port 8000/5173 already bound | yesterday's server/vite still up | `ps aux \| grep [u]vicorn`, kill by PID, restart on the right branch |
| `openai/gpt-oss` model 404s on Groq | roster moved (happened 2026-09-04) | re-list `/openai/v1/models`, update `models.yaml` |
| MPA/EEZ "unavailable" in answers | no keyless polygon exists (checked 2026-09-07) | expected; re-run `ingest/static/mpa_eez.py` before assuming otherwise |

## Sharp edges for demo day

- `active_alerts.yaml` older than 24 h degrades every safety verdict. Refresh
  it the morning of the demo even when empty ("looked, nothing in force").
- Chennai queries refuse: the city sits outside the box (`lat_max: 12.0`) by
  design. Don't demo from Chennai.
- Free-tier mornings are congested; run the paid tier for any judged demo.
- The UI shows one opaque spinner per turn; per-stage progress is not wired
  (turn trace has the notes, nobody renders them live yet).
