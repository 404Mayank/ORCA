# ORCA — Marine Ecosystem Reasoning with Collaborative Agents

Agentic marine advisory for fishermen on the Tamil Nadu coast.
SIH Problem Statement 26176 · ISRO / Department of Space · Disaster Management.

Ask *"is it safe to take my FRP boat out from Nagapattinam tomorrow morning?"*
and get a verdict, the numbers behind it, and a time to be back by — every
figure traceable to the tool call that produced it.

## The governing rule

> The LLM interprets, plans, asks, hypothesises and narrates.
> **Code retrieves, computes, thresholds and verifies.**

An LLM never produces a number attached to a safety claim. It is used exactly
twice — once to turn the question into a plan, once to turn a finished answer
into English — and code gates both ends: the plan is validated before it runs,
the prose is checked digit-by-digit against the object before it is shown.

Everything in between is deterministic. There is **no trained model**. A fitted
safety classifier could not tell a judge why 2.5 m; `config/risk_thresholds.yaml`
can, with a citation per threshold.

## Quick start

```bash
pip install -e ".[geo,dev]"
python scripts/refresh_cache.py     # populate the ingest cache (needs network)
python scripts/refresh_cache.py --replay   # cyclone archives, for the demo
python -m pytest -q                 # 284 passing
uvicorn orchestrator.main:app      # API at http://127.0.0.1:8000/docs

# frontend, in a second terminal
cd frontend && npm install && npm run dev   # http://localhost:5173
python scripts/try_agents.py        # run all four query types on live data
```

`data/` is gitignored and holds the cache, so a fresh clone starts empty.
`refresh_cache.py` is what fills it. Run it nightly, and once before any demo.

The deterministic core needs no API key and no network:

```bash
python -m pytest -q tests/test_schemas.py tests/test_verifier.py tests/test_thresholds.py
```

An LLM key is optional. Copy `.env.example` to `.env` and add a `GROQ_API_KEY`
or `ANTHROPIC_API_KEY` to enable the planner and narrator; without one, the
system falls back to deterministic template rendering, which is always correct
and merely less fluent. **It never loses the ability to answer.**

## Pipeline

```
question (English)
  → language adapter          stub: detect returns "en", translate is identity
  → LLM #1  intent + planner  one call, two blocks
  → validate_plan()           deterministic gate, before anything executes
  → executor                  runs the DAG in parallel, logs every call
  → tools/                    all real computation
  → agents/                   four domain agents return typed fragments
  → synthesis_agent           assembles the Recommendation object
  → verifier                  every number checked against the tool call log
  → LLM #2  narrator          rewrites; number guard discards it if it drifts
  → answer
```

## Data

Everything is fetched by `ingest/` on a schedule and cached. **No tool ever
fetches during a user query** — a query that touches the network is a query
that fails when the venue wifi drops.

| Layer | Source | Auth |
|---|---|---|
| Waves, wind, tides, SST, currents | Open-Meteo Marine + Forecast | keyless |
| Near-real-time SST | NOAA CoastWatch `jplMURSST41` (MUR L4, 0.01°) | keyless |
| Near-real-time chlorophyll | NOAA CoastWatch VIIRS, DINEOF gap-filled | keyless |
| Monthly baseline | `nesdisVHNSQchlaMonthly`, 2012– | keyless |
| India–Sri Lanka IMBL | 1976 treaty text, 27 positions, digitised | in repo |
| Thresholds | `config/risk_thresholds.yaml`, cited per value | in repo |

**Not connected:** GEBCO bathymetry, MPA and EEZ geometry, INCOIS wave alerts.
See `PROGRESS.md` for what each one blocks.

> **Alerts.** Cyclones come from GDACS (live, keyless). INCOIS high-wave and
> swell-surge advisories have no machine-readable feed and come from
> `config/active_alerts.yaml`, which a human updates before a shift. A type
> with no reachable source is never reported as clear — `active_alerts`
> returns `FAILED`, not an empty list, and `compute_risk_score` forces `no_go`
> on it.

## Layout

```
config/         bbox, datasets, risk_thresholds, models, active_alerts (yaml)
core/schemas/   recommendation, intent, plan, tool_io    <- imports nothing
core/           provenance, units, config
ingest/         open_meteo, erddap, climatology, static/
tools/          registry + ocean, weather, geo, risk     <- all computation
agents/         base + 4 domain agents, planner, synthesis, narrate
orchestrator/   executor, validate_plan, verifier, llm/
language/       detect, translate, templates/en
scripts/        refresh_cache, gen_types, try_agents, try_planner, try_narration
frontend/src/   React + MapLibre; types.ts is GENERATED from core/schemas/
docs/           verified_sources.md, ideal_answers/
```

## Documents worth reading first

- **`docs/ONBOARDING.md`** — new here? Start here (setup, secrets, run, map)
- **`CLAUDE.md`** — the architecture and the rules that do not bend
- **`PROGRESS.md`** — what actually works today, what blocks what
- **`docs/DECISIONS.md`** — why things are the way they are, one line each
- **`docs/RUNBOOK.md`** — operate and demo (ingest, tiers, troubleshooting)
- **`CONTRIBUTING.md`** — the workflow contract for sending work back
- **`docs/verified_sources.md`** — every external fact, with reproduction steps
  and the ones that turned out to be false
- **`config/risk_thresholds.yaml`** — the file to open when a judge asks
  "why 2.5 m?"
