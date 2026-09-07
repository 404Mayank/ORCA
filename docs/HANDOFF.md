# ORCA Handoff — everything a new agent (or human) needs

SIH Problem Statement 26176: agentic marine advisory for fishermen on the
Tamil Nadu coast. Read `CLAUDE.md` for the rules and `ARCHITECTURE.md` for
the design; this file is the **current operational state** — what runs
where, what is true right now, what is left, and what will bite you.

> **Status as of 2026-09-07 EOD:** master current through PR #13 (merged).
> Open: #12 (discovery agent, Ritvik06-dev) — **HELD, do not merge**
> (contradicts the no-discovery-agent architecture; needs owner call).
> Suite: **423 passed / 11 skipped**. UI + API verified live with screenshots.

---

## 1. The governing rule (non-negotiable)

> The LLM interprets, plans, asks, hypothesises and narrates.
> Code retrieves, computes, thresholds and verifies.

The LLM must never produce a number attached to a safety claim. Numbers come
from Python tools and are checked against the tool-call log by
`orchestrator/verifier.py` **before** any narration. Corollaries live in
`CLAUDE.md` — no guessing locations/vessels (ask), untested causal
hypotheses are dropped not reported, unsure-means-it-belongs-in-code.

## 2. Fixed scope (do not expand)

- **Box:** lon 78.5–82.0, lat 8.0–12.0 (South Coromandel). Grid 0.05°, EPSG:4326.
- **Five queries:** `pfz_locate`, `safety_assess`, `geofence_check`,
  `causal_explain`, `conditions_report` (read-only; safety phrasing re-routes
  to `safety_assess` in code).
- **English only.** Tamil later = second object in
  `frontend/src/i18n/strings.ts` + backend adapter stub (both already shaped).
- No new agents, sources, or query types. No discovery agent — catalogue
  lookup is deterministic (`resolve_datasets()` in tools).

## 3. Running it

| What | How | Notes |
|---|---|---|
| API | `.venv/bin/python -m uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000` | single worker; see §9 |
| UI | `cd frontend && npm run dev -- --port 5173 --strictPort` | `--strictPort`: a squatter must fail loudly, never slide to 5174 |
| Health | `curl localhost:8000/health` | tools registered/implemented |
| Readiness | `curl localhost:8000/readiness` | cache ages + provider flags |
| Full suite | `.venv/bin/python -m pytest -q` (~11 s) | hermetic: `tests/conftest.py` scrubs keys; minutes = a test hitting network |
| Frontend | `cd frontend && npx tsc -b && npm run build` | both must pass |
| Logs | `/tmp/orca-api.log`, `/tmp/orca-ui.log` | nohup'd dev servers |

**Restart recipe (learned the hard way):** never bare `pkill -f` with a
pattern appearing in your own command — it suicides. Use
`pkill -9 -f "[b]in/vite"` (brackets) and PID kills for uvicorn; verify with
`ss -tln | grep -E "8000|5173"` before starting fresh.

**Vite poisoned transform (recurring):** after file edits the dev server can
serve a stale broken module (`does not provide an export named 'default'`
with a clean `tsc`). Recovery: kill vite, restart with `--force`, re-verify
with `/tmp/shot.py`. If it keeps recurring, suspect the watcher, not the code.

## 4. API surface

| Endpoint | Purpose |
|---|---|
| `POST /chat` | One blocking turn. The fallback and the CLI path. |
| `POST /chat/stream` | Same turn over SSE: allowlisted stage frames, full `ChatResponse` as terminal `done`. Falls back to `/chat` on transport trouble. |
| `GET /session/{id}`, `DELETE /session/{id}` | History for the rail; real forget (called on new-session/fresh sends). |
| `GET/POST /settings` | Tier + 8 knobs (see §5). Overrides are process-local; restart returns to defaults. |
| `GET /geo/boundaries` | IMBL polyline from the tool's own treaty source + provenance. Static. |
| `GET /replay`, `POST /replay/{id}` | Archived cyclone through production tools (see §7). |
| `GET /health`, `/readiness` | Liveness; cache ages + provider flags + recovery hint. |

**Concurrency contract:** replay swaps `CACHE_DIR` process-globally.
`/chat` + `/chat/stream` answer **503 + Retry-After** while a replay owns
the lock (tested). Single-worker deployments only.

## 5. Settings knobs (all live, all tested)

Tier `free|fast|paid` (source badge: app/env/**default**) plus:
`deliberating`, `context_ttl_min` (5–480), `pending_ttl_min` (1–120),
`max_rounds` (0–4), `max_added_steps` (0–8), `template_fallback`,
`llm_timeout_s` (5–120), `step_timeout_s` (5–120). Out-of-range 422s.
`POST {"reset": [...]}` returns knobs to compiled defaults. UI surfaces
tier, reasoning, memory presets, safety net, follow-up checks; the rest are
API-only by choice. **Never expose:** API keys, `risk_thresholds.yaml`,
bbox/datasets, safety-critical flags, per-role model ids (full list with
reasons in the scout report; ask for it).

## 6. Model lineup (2026-09-07, owner-set)

- **Paid/Go:** `muse-spark-1.3-contributor` → `1.2-contributor` →
  `glm-5.3-flash` → `deepseek-v4-flash` (spark→`/responses`, rest→chat),
  then zen → groq provider fall-through.
- **Free/Zen:** `1.3-contributor-free` → `1.2-contributor-free`, then Go, then groq.
- **Groq:** `gpt-oss-120b` planner/narrator, `20b` deliberator (strongest verified).
- Old chains (kimi-k3 etc.) kept as documented substitutes in
  `config/models.yaml`, not live. Re-list any roster before a demo —
  unlisted ids fail as silent fall-through. `tests/test_opencode.py` pins
  the walk order; update it with the lineup, never around it.

Keys: `OPENCODE_API_KEY`, `OPENCODE_GO_API_KEY`, `GROQ_API_KEY`,
`ORCA_TIER` in `.env` (auto-loaded at import; tests scrub them).

## 7. Data state

- **Cached:** weather (Open-Meteo, ~2–4 h old via nightly refresh),
  alerts (GDACS, 0 in force), bathymetry, Fengal replay archive (120 h/layer,
  gitignored under `data/replay/`).
- **Missing:** SST + chlorophyll — CoastWatch ERDDAP SSL-fails from this
  network, so PFZ/causal stay degraded. **Retry from better wifi before any
  demo:** `.venv/bin/python scripts/refresh_cache.py --ocean`.
- Live demo proves little (September sea is calm → `go`); the Fengal replay
  (`POST /replay/fengal`, `no_go` 71.5 h pre-landfall) is the demo opener.

## 8. Frontend map (`frontend/src/`)

- **Views:** Bridge (hero, 4 cards, composer, chips) / Thread (answers +
  side panel) / rail sheets (Conversations, Saved, Alerts, Settings, Replay).
- **Rules:** all copy in `i18n/strings.ts` (nothing user-facing hardcoded
  elsewhere); `types.ts` is generated (`scripts/gen_types.py`, pinned by
  `test_types.py`); bridge entries start **fresh** sessions, thread
  continues; numbers render only from `ChatResponse`.
- **Honesty surfaces:** verdict stamp (non-tappable), `✓ N verified` meta,
  evidence trail, joined data-status rows, weather-only feed label.
- **Safety nets:** ErrorBoundaries on sheets + side panel, reqId invalidation
  mid-flight, origin validation before mapping, busy dimming everywhere.
- **Harness:** `/tmp/shot.py` (+ thread/sheet variants) via Playwright/Chromium
  in `/tmp/pw` venv — desktop + 390px with console-error capture. Use it;
  eyeballing is how regressions shipped before.

## 9. Key contracts (backend)

- `core/schemas/` frozen; everything imports from it, it imports nothing.
- `tools/registry.py` drives the planner prompt programmatically.
- `orchestrator/validate_plan.py` runs before execution (12-step cap,
  fallback plans); `orchestrator/verifier.py` before narration
  (hallucination-rejection test exists — extend it, never weaken it).
- `strip_numbers()` on all LLM free text (deliberation, chat, concerns).
- `orchestrator/progress.py` allowlist: stages stream, prose never does
  (narration emits source-only; UI reveal is post-guard).
- `/chat` never 5xxes domain failures (200 + state), except 503 during replay.

## 10. What's left (grouped, honest sizes)

- **Small:** settings reset-all button (API ready), `vis_km` replay column,
  focus restore on sheet close, thread-title drift, `by_claim` visual check.
- **Medium:** route legs on map (needs synthesis waypoint claims +
  verifier attention), replay UI polish, Tamil adapter + strings swap.
- **Structural:** MPA/EEZ geometry, INCOIS high-wave feed (operator table
  stands in), scheduled ingest, Gaja replay dataset.
- **Explicitly declined** (reasons on file): per-model pickers, threshold
  sliders, MAX_REQUESTS knob, token-streaming narration, multi-worker replay.

## 11. Git workflow

Branches `feat/s*` on fork `404Mayank/ORCA`, PRs to `mannangrover/ORCA`
master (stacked; #3–#11 and #13 merged). Commit only with 423-green suite +
`tsc` + build + ruff-no-new-violations. Standing orders from the owner:
glm-5.3-flash critiquer reviews every feature after it lands; subagents get
tight file allowlists (one writer per file set).
