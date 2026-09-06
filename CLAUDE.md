# ORCA — Marine Ecosystem Reasoning with Collaborative Agents

Agentic marine advisory system for fishermen on the Tamil Nadu coast.
SIH Problem Statement 26176, ISRO / Department of Space, theme Disaster Management.
Hackathon project under time pressure. Bias toward working code over polish.

## Scope (fixed, do not expand)

**Geographic:** South Coromandel box only.

```
LON_MIN = 78.5   LON_MAX = 82.0
LAT_MIN = 8.0    LAT_MAX = 12.0
```

Chennai to Rameswaram, including Palk Bay and the Gulf of Mannar. Covers the
cyclone corridor, the India–Sri Lanka IMBL, and the Gulf of Mannar MPA.
The architecture is region-agnostic; only the data cache is scoped.

**Target grid:** 0.05° regular lat/lon, EPSG:4326. 81 lats × 71 lons.
Everything is regridded onto this at ingest time.

**Queries (four only):**
1. `pfz_locate` — where is the nearest Potential Fishing Zone today
2. `safety_assess` — is it safe to venture out tomorrow morning
3. `geofence_check` — which zones must be avoided
4. `causal_explain` — why has fish productivity declined here

**Language:** English only in phase one. The language adapter exists as a
pass-through stub so Tamil is a one-adapter change later. Do not delete it.

## THE GOVERNING RULE

> The LLM interprets, plans, asks, hypothesises and narrates.
> Code retrieves, computes, thresholds and verifies.

The LLM assembles the inputs; a deterministic function returns the verdict.

**The LLM must never produce a number attached to a safety claim.** Not distance
to a PFZ, not whether a point is inside the IMBL, not wave height, not the risk
score, not route waypoints. Those come from Python, and the LLM reports them.

Corollaries:
- Planning is not adjudication. The planner emits a step calling
  `compute_risk_score()`. It does not weigh a 2.8 m swell against a vessel class.
- For causal queries the LLM proposes hypotheses, deterministic code tests them,
  and untested hypotheses are DROPPED, not reported.
- Never guess a location or vessel class for a safety question. Ask instead.

If you are ever unsure whether something belongs in the LLM or in code, it
belongs in code.

## Architecture

```
User query (English)
  -> Language adapter (stub: detect returns "en", translate is identity)
  -> Intent + planner agent (ONE LLM call, two blocks: intent, plan)
  -> validate_plan()  [deterministic, runs before execution]
  -> Agent groups in parallel: ocean | weather | geospatial | risk+route
  -> Tool layer (typed Python functions, all real computation)
  -> Data plane (Supabase + Zarr)
  -> Recommendation object (assembled from typed fragments)
  -> Verifier (every number checked against the tool call log)
  -> Synthesis + narration -> UI
```

### Agents (seven)

| Agent | Owns |
|---|---|
| `intent_planner_agent` | slots + task DAG; returns plan, clarification, or refusal |
| `ocean_agent` | SST, chlorophyll, thermal fronts, PFZ candidates, anomalies |
| `weather_agent` | waves, wind, tides, lightning, cyclone bulletins |
| `geospatial_agent` | nearest point, distance/bearing, geofence, route grid |
| `risk_agent` | invokes `compute_risk_score()` and `optimise_route()` |
| `synthesis_agent` | assembles the recommendation object, narrates |
| verifier | not an LLM; deterministic check in `orchestrator/verifier.py` |

There is no discovery agent. Catalogue lookup is `resolve_datasets()` in the
tool layer, deterministic.

### Agents return typed fragments, never prose.

## Key contracts

### `core/schemas/recommendation.py` — freeze this first

Everything imports from `core/schemas/`. It imports from nothing.

Rules that are not negotiable:

1. **Every value is a range**, never a scalar: `{min, max, unit, qualifier}`.
   Real marine data is "15–20 knots gusting 25".
2. **Every claim has `kind`**: `observed` | `derived` | `inferred`.
   A chlorophyll reading is observed. "Moderate sea state" is derived by rule.
   "Skipjack will aggregate here" is inferred. The verifier treats them
   differently.
3. **Every claim points at evidence; every evidence entry points at a
   `tool_call_id`.** This link is what makes the verifier and the explainability
   drawer real.
4. **Claims are slot templates, not finished prose.**
   Store `{"swell_m": 2.8, "dir": "SE"}` plus a template. Never store the
   English sentence. Translating a finished safety sentence corrupts numbers.

Blocks: `verdict`, `headline`, `claims[]`, `drivers[]` (verdicts only, each with
observed vs threshold), `hypotheses[]` (causal only, each with test result and
`supported` bool), `negative_findings[]` ("no cyclone active" is first-class,
not a missing field), `operational_guidance[]`, `window`, `spatial_context`,
`evidence[]`, `assumptions[]`, `confidence {overall, by_claim, basis}` (basis is
required), `alternatives[]`, `visual_layers[]`, `caveats[]`, `reasoning_trace`.

The schema must survive: a causal query with no verdict; a failed tool producing
a degraded partial answer; a refusal (outside the box); a follow-up turn
inheriting spatial context from the previous turn.

### `tools/registry.py`

Name -> function + Pydantic in/out schema. The registry is injected into the
planner prompt **programmatically**. Never hand-maintain a tool list inside a
prompt file; it will drift within a week.

### `orchestrator/validate_plan.py`

Runs before any execution. Checks: every tool exists in the registry; every
argument matches its Pydantic schema; no cycles; all `depends_on` ids exist;
every `$sN` reference resolves; step count under cap (12). On failure, one
replan attempt with the error fed back, then fall back to a hardcoded plan for
that intent type.

### `orchestrator/verifier.py`

Walks every claim, confirms each number appears in the referenced tool output
log, rejects for revision if not. Stricter for `observed` than `inferred`.
This is our headline technical claim — it must actually work, and there must be
a test that feeds it a hallucinated number and asserts rejection.

## Tech stack

- Python 3.11, FastAPI, Pydantic v2, asyncio, httpx
- Planner + causal reasoning: Claude Sonnet 4.5 or GPT-4.1
- Intent + narration: Llama 3.3 70B / Mistral Large via Groq
- Fallback: Ollama + Llama 3.1 8B (the venue will lose internet)
- Geospatial: Shapely, GeoPandas, pyproj
- Gridded: xarray, Zarr, rioxarray, netCDF4
- Routing: NetworkX or hand-rolled A* over the cost grid
- **Database: Supabase** (Postgres 15). Enable PostGIS:
  `create extension if not exists postgis;`
  Use the connection pooler on port 6543, not 5432 — free-tier connection
  limits will not survive six developers plus a worker.
- Object storage: Supabase Storage for Zarr chunks and clipped layers
- Frontend: React 18, Vite, TypeScript, Tailwind, MapLibre GL JS, Recharts

**Gridded fields do NOT go in Postgres.** SST, chlorophyll, wave, wind and
climatology live as Zarr, mirrored to local `data/` for fast demo reads.

## Layout

```
config/         bbox, datasets, risk_thresholds, models (yaml)
core/schemas/   recommendation, intent, plan, tool_io   <- freeze first
core/           provenance (tool_call_id logging), units, supabase_client
ingest/         sources, static, transform, catalogue, climatology
tools/          registry + ocean, weather, geo, risk
agents/         one file per agent + prompts/ as .md files
orchestrator/   main (FastAPI), routes, executor, validate_plan, verifier, llm/
language/       detect (returns "en"), translate (identity), templates/en
alerts/         worker, rules, simulate_track
frontend/src/   components, api, hooks, types
data/           gitignored: zarr, raw, static, replay
fixtures/       mock tool outputs — the team codes against these
```

## Conventions

- Prompts live in `agents/prompts/*.md`, never as inline strings.
- Frontend TypeScript types are generated from the Pydantic schemas. Do not
  hand-write them; they will drift.
- `config/risk_thresholds.yaml` carries a **citation comment per threshold**,
  traceable to INCOIS small-vessel alert criteria. It is a deliverable, not a
  config file. It is what we open when a judge asks why 2.5 m.
- Units are explicit everywhere, stored as xarray attrs and Pydantic fields.
  Half the failure modes in this project are unit confusion.
- Every external fetch records provenance: source URL, retrieval timestamp,
  native resolution, exact query.
- Never fetch from an external API during a user query. Everything is
  pre-cached by `ingest/` on a schedule.
- Idempotent ingest scripts. Re-running must not corrupt or duplicate.

## Data sources

Static (fetch once, clip to box, commit to repo):
- GEBCO 2024 bathymetry
- Natural Earth / OSM coastline and land mask
- Marine Regions v12 EEZ; India–Sri Lanka IMBL (MRGID 8480, GML)
- US State Dept "Limits in the Seas" No. 77 — actual treaty coordinates for the
  1976 Gulf of Mannar (13 turning points) and Bay of Bengal boundaries, and the
  1974 Palk Bay historic waters agreement. Digitising from the treaty is better
  than a shapefile and is a strong talking point.
- Protected Planet / WDPA — Gulf of Mannar MPA
- INCOIS fish landing centre points

Forecast (nightly refresh):
- **Open-Meteo Marine API** — keyless, no auth. Wave height/direction/period,
  wind-wave and swell components, `sea_level_height_msl` (this is our tide
  signal), SST, ocean currents. Wire this FIRST; it unblocks everything.
- Open-Meteo forecast endpoint — wind, gusts, precipitation, visibility.
- INCOIS Ocean State Forecast — MWW3 waves to 0.05°, ROMS 0.125° circulation,
  TASK-2000 tides. An upgrade over Open-Meteo, not a blocker.

Satellite (daily, cloud-affected):
- INCOIS ERDDAP, `https://erddap.incois.gov.in/erddap/`. RESTful; change the
  file extension for format. Query `allDatasets` to enumerate. Known ids:
  `incois_oceansat2_datasets` (chl-a, KD490, total suspended matter, ~0.039°),
  `incois_tmi_3day_datasets` (SST + wind, 0.25°),
  `NOAA_AVHRR_AMSR_datasets`, `ascat_daily_datasets`.
  Total suspended matter feeds the turbidity hypothesis — keep it.
- Copernicus Marine L4 gap-filled SST and chlorophyll. **Required**, because
  monsoon Coromandel is overcast for days and L2 products will be full of holes
  during the demo.

Climatology (needed for `causal_explain`, unanswerable without it):
- 10 years of monthly SST and chlorophyll reduced to per-pixel per-month mean
  and std. Keep only the reduced arrays.
- Multi-year alongshore wind for an upwelling index baseline.
- CMFRI landing statistics for Nagapattinam / Cuddalore if obtainable.

Alerts: IMD cyclone bulletins and lightning, INCOIS high-wave and swell-surge.
Normalise to `{type, severity, zone, issued_at, valid_until, authority, text}`.

**The official INCOIS PFZ advisory is text and maps, not a clean API.** We
derive PFZ candidates ourselves from SST fronts plus chlorophyll and treat the
official advisory as corroboration. Deriving it is more impressive than fetching
it, and it is what makes the "explain your reasoning" requirement answerable.

## Handling degraded data

Cloud gaps are the single biggest data risk. Design for them:
- prefer L4 gap-filled products
- fall back to 3-day then 7-day composites
- carry `data_age_days` and `clear_pass_fraction` alongside every gridded field
- surface it honestly in `confidence.basis`, e.g. "7-day composite, last clear
  pass 3 days ago"

Never silently substitute stale data for fresh.

## Demo assets

`data/replay/gaja_2018/` — hourly wave, wind and alert data for the five days
around Cyclone Gaja's landfall (Nov 2018), from Open-Meteo's archive API.
A live demo on a calm day proves nothing. Replaying a real cyclone and watching
the risk score go red 36 hours before landfall proves everything.

## Open items

- [ ] Write the safety-verdict ideal answer by hand, then validate the
      `verdict / drivers / window / alternatives` block against it. This is the
      block a judge will poke at and it is currently unvalidated.
- [ ] Freeze `core/schemas/` and generate frontend types from it.
- [ ] Populate `fixtures/` on day one so the team is not blocked on ingestion.
- [ ] `config/risk_thresholds.yaml` with a citation per threshold.
- [ ] Test that feeds the verifier a hallucinated number and asserts rejection.

## Working style

- Do not add agents, data sources, or query types beyond what is listed here.
- When a task is ambiguous, ask rather than assume — same rule we apply to the
  system itself.
- Prefer small, testable functions in `tools/` over clever agent prompting.
- If something cannot be done properly in the time available, say so and
  propose the honest degraded version. Do not fake data.
