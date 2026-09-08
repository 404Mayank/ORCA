# Onboarding: from zero to a green turn in 20 minutes

Read in this order: `CLAUDE.md` (rules) → `ARCHITECTURE.md` (design) →
this file (hands) → `docs/RUNBOOK.md` (operate) → `docs/DECISIONS.md`
(why things are the way they are). `PROGRESS.md` is the chronological log;
skim its dated headers, don't read it linearly.

## 1. Setup

```bash
cd /home/zyrus/Desktop/Projects/ORCA
git checkout master   # merged through #13; see docs/HANDOFF.md for state
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"  # add [geo] for geo/gridded tests
cp .env.example .env               # then fill keys (see §2)
```

`pythonpath = ["."]` is set for pytest, so no install is strictly needed for
the core suite — but install anyway; `test_types.py` shells out to scripts
that import the app.

## 2. Secrets: `.env` auto-loads, and that bites

`core/env.py` loads `.env` **at import time** (existing env wins, blanks
ignored). Consequences, both learned the hard way:

- The app needs no export dance: keys in `.env` are live for uvicorn, scripts
  and ad-hoc probes alike.
- The **test suite scrubs credentials** (`tests/conftest.py::hermetic_env`)
  because auto-load once made collaboration tests deliberate against live
  models (minutes per test, real spend). If the suite suddenly takes minutes
  instead of ~8 s, something is reaching the network — check for a test that
  bypasses the scrub, not the code.
- Never print a key. Check presence by length only:
  `python3 -c "import os; print(len(os.environ.get('OPENCODE_API_KEY','')))"`
  after importing anything from `core` (which triggers the load).

Keys in play: `OPENCODE_API_KEY` (Zen), `OPENCODE_GO_API_KEY` (Go gateway),
`GROQ_API_KEY` (fallback), `ORCA_TIER` = `free|fast|paid` (default `paid`),
`SUPABASE_*` (RAG store only), `COPERNICUS_*` (unused; CoastWatch covers us).

## 3. Run it

```bash
# API (port 8000, /docs for the playground)
.venv/bin/python -m uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000
# UI (a second terminal; takes the next free port if 5173 is held)
cd frontend && npm install && npm run dev
# One turn, timed, no UI needed:
curl -X POST localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"query": "What are the weather and sea conditions near Nagapattinam?"}'
```

Expected latency (measured 2026-09-07): ~12 s paid tier, ~60–110 s free
(shared-queue variance, not compute). A turn that hangs for minutes with no
log movement is a queued/starved model chain, not a deadlock — check which
chain id is serving before debugging code.

## 4. Test it

```bash
.venv/bin/python -m pytest -q          # ~8 s hermetic; 380+ passed, 15 skipped
.venv/bin/python -m pytest tests/test_x.py -q
.venv/bin/ruff check <files-you-touched>   # repo-wide has pre-existing debt; keep yours clean
```

Skips are satellite-cache-dependent (no `data/` ocean layers) — honest skips,
not failures. `data/` is gitignored; populate with
`.venv/bin/python scripts/refresh_cache.py --weather --alerts` (fast) and add
`--ocean` when you need the satellite layers (slow).

## 5. Where things live (the 2-minute map)

- `orchestrator/turn.py::run_turn` — the whole turn, one function. Start here.
- `orchestrator/llm/client.py` — providers, chains, transports. Never raises.
- `agents/intent_planner_agent.py` — planner + all Gates (0/0b/1/1a/1b/2).
- `tools/` — every number originates here. `tools/registry.py` is the index.
- `agents/synthesis_agent.py` — `_build_*` per query type; verifier runs after.
- `config/models.yaml` — tiers, chains, order. `config/risk_thresholds.yaml`
  is the cited-thresholds deliverable, not config.
- `frontend/src` — `App.tsx`, `api/client.ts`, `components/`; `types.ts` is
  GENERATED (`scripts/gen_types.py`, pinned by test).

## 6. Sending work back

Stacked branches `feat/s*` → PRs into `mannangrover:master` (fork
`404Mayank/ORCA` is the `fork` remote). See `CONTRIBUTING.md` for the
contract: plan → critiquer → implement → stash-A/B zero-regression proof →
push → PR. Don't commit `data/`, `.env`, `.venv`, or `frontend/dist`.
