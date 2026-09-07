# ORCA — Architecture

Marine advisory system for fishermen on the Tamil Nadu coast, between Chennai
and Rameswaram. SIH Problem Statement 26176, ISRO / Department of Space.

This document explains what the system is made of, what each agent is for, where
every number comes from, and how the agents talk to each other. It is written to
be read start to finish by someone who has never opened the code.

---

## 1. The one rule everything else follows

> **The LLM interprets, plans, asks, hypothesises and narrates.
> Code retrieves, computes, thresholds and verifies.**

An LLM may never produce a number attached to a safety claim. Not a wave height,
not a distance to a fishing zone, not a risk score, not a coordinate, not a route
waypoint. Those come from Python functions reading cached measurements, and the
model reports them.

This is not a prompt instruction we hope holds. It is enforced in three places:

| Guard | Where | What it does |
|---|---|---|
| `validate_plan()` | `orchestrator/validate_plan.py` | runs **before** any tool executes: every tool exists, every argument type-checks, no cycles, no arithmetic in a `$sN` reference |
| `verify()` | `orchestrator/verifier.py` | runs **before** any prose is produced: walks every number in the answer back to the tool call that returned it |
| `strip_numbers()` | `agents/deliberate.py` | removes digits from any free text an LLM writes outside a verified claim — agent concerns, chat replies |

If verification fails, **narration is not attempted at all**. There is no path
in the codebase where an unverified object reaches a language model, because a
fluent rendering of a wrong object is the most dangerous output the system could
produce.

The `✓ 15 numbers verified` badge in the UI is the visible end of this chain.

---

## 2. The shape of one turn

```
  user types a question
        │
        ▼
  ┌───────────────┐   small LLM call (~460 token prompt)
  │    ROUTER     │   "is this conversation, or is it about the sea?"
  └───────────────┘
        │
   chat │ query
        │   └──────────────────────────────────┐
        ▼                                      ▼
   reply, done                        ┌─────────────────┐  large LLM call
   (no tools, numbers stripped)       │ INTENT +PLANNER │  (~2.6k prompt +
                                      └─────────────────┘   tool catalogue)
                                               │
                                               ▼
                                        validate_plan()          ← deterministic
                                               │
                                               ▼
                              ┌────────────────────────────────┐
                              │  EXECUTE plan, round 1         │
                              │  ocean │ weather │ geo │ risk   │  parallel
                              └────────────────────────────────┘
                                               │
                                               ▼
                              ┌────────────────────────────────┐
                              │  DELIBERATE — one LLM call per │
                              │  agent, on its own findings    │
                              └────────────────────────────────┘
                                               │
                              agents request extra steps of each other
                                               │
                                               ▼
                              ┌────────────────────────────────┐
                              │  EXECUTE added steps, round 2  │
                              └────────────────────────────────┘
                                               │
                                               ▼
                                    build_recommendation()       ← deterministic
                                               │
                                               ▼
                                          verify()               ← deterministic
                                          │        │
                                     fail │        │ pass
                                          ▼        ▼
                                   refuse to    NARRATE (LLM)
                                   show it      + guard against drift
```

Everything lives in one function — `orchestrator/turn.py::run_turn()` — so the
HTTP route, the CLI scripts and any future channel run **exactly the same path**.
A demo that works over HTTP but not from a script is a demo with two behaviours
and no way to tell which one a judge saw.

---

## 3. The agents

Seven, and one of them is not an LLM at all.

### 3.1 Router — `agents/prompts/router.md`

**Purpose.** Decide whether a turn needs the machinery at all.

A greeting, a question about what ORCA is, or something unparseable does not need
fourteen tool schemas loaded into a prompt to be answered. The router asks one
small question with a small prompt, and only a turn genuinely about the sea goes
on to pay for the planner.

**Why it exists.** Arithmetic, not elegance. The planner prompt is ~2,600 tokens
plus the tool catalogue, billing ~4,500 per call. A Groq free-tier key allows
8,000 tokens per minute — so one planner call consumed most of a minute's budget
and a second inside the same minute was refused. Greetings went from 4,500 ms to
~300 ms once they stopped loading the catalogue.

**Uses an LLM.** Yes. There is no keyword list in it. When it is unsure it is
instructed to choose `query`, because spending tokens is cheaper than losing a
safety question to small talk.

**Output.** `{"kind": "chat", "text": ..., "suggestions": [...]}` or
`{"kind": "query"}`. A chat reply passes through `strip_numbers()` — it has
called no tool, so any digit in it is unverifiable by construction.

### 3.2 Intent + planner agent — `agents/intent_planner_agent.py`

**Purpose.** Work out what is being asked, and lay out which tools answer it.

One LLM call returns two things: the **intent** (query type, place, time window,
vessel class) and the **plan** (a DAG of tool calls). It does not answer the
question. It arranges the calculation.

**Five query types:**

| type | the user is asking |
|---|---|
| `safety_assess` | is it safe to go out |
| `pfz_locate` | where is the nearest good fishing zone |
| `geofence_check` | which waters must be avoided |
| `causal_explain` | why has the catch fallen off |
| `conditions_report` | tide, weather and alert status at a place — read-only, no verdict |

**Slots.** `spatial_reference`, `time_window`, `vessel_class`. The model may fill
`spatial_reference.name` — the *word* the user said. It may **not** fill `lat` or
`lon`; the `resolve_place` tool does that from the gazetteer.

For `safety_assess`, place and vessel are both required. Missing either produces
a **clarification**, never a guess, because the vessel class selects the
thresholds the verdict comes from. For `conditions_report`, place alone is
required; vessel is never asked for, and safety phrasing re-routes to
`safety_assess` (Gate 1a) so the vessel gate cannot be bypassed.

**Gates applied after the model answers**, in code:

- **Gate 0** — a greeting inherits no slots. It still reaches the model; what it
  does not get is `carry_these_slots`, so it cannot silently re-ask the previous
  question.
- **Gate 0b** — a chat turn returns immediately, before inheritance or slot
  checks. There is nothing to gate: no tool will run and no number claimed.
- **Gate 1** — missing safety-critical slot → forced clarification, whatever
  state the model returned.
- **Gate 1a** — the reporting/advising fence. A `conditions_report` intent
  with safety-decisive phrasing in the raw query is rewritten to
  `safety_assess` before Gate 1, so the vessel gate fires on it.
- **Gate 1b** — the model may not declare one of the five types out of scope.
  When it does, the reason is ours, and the refusal says so honestly.

**Fallback tiers**, in decreasing confidence:

1. the planner LLM
2. **conversational retry** — if the planner's JSON will not parse twice, the
   model is demonstrably up and only the *shape* was unusable, so it is asked
   again with the schema removed
3. the previous turn's context
4. keyword classification (`agents/keyword_intent.py`) — proposes a query type
   only, never a coordinate
5. an honest clarification

### 3.3 Ocean agent — `agents/ocean_agent.py`

**Owns:** sea-surface temperature, chlorophyll-a, thermal fronts, PFZ candidates,
anomalies.

| tool | what it computes |
|---|---|
| `thermal_front` | SST gradient magnitude across the grid; a front is a gradient above threshold |
| `chl_anomaly` | log-normal anomaly of chlorophyll against a ≥5-year monthly climatology, in σ |
| `pfz_candidates` | fronts ∩ elevated chlorophyll, ranked, with distance from origin |

### 3.4 Weather agent — `agents/weather_agent.py`

**Owns:** waves, wind, tides, cyclone advisories.

| tool | safety-critical | what it returns |
|---|---|---|
| `wave_forecast` | ✔ | hourly significant height, period, direction, wind-wave/swell split, steepness |
| `wind_forecast` | ✔ | sustained wind, gusts, direction, visibility |
| `tides` | | sea level over the window, high and low water times |
| `active_alerts` | ✔ | advisories in force. **An empty list is a first-class finding**, not a missing field — "no cyclone active" is something the system asserts, having checked |

### 3.5 Geospatial agent — `agents/geospatial_agent.py`

**Owns:** turning names into coordinates and coordinates into geometry.

| tool | what it does |
|---|---|
| `resolve_place` | gazetteer lookup — the only path from a name to a coordinate |
| `distance_bearing` | geodesic distance and initial bearing (pyproj) |
| `nearest_landing_centre` | closest landing centre with distance and bearing |
| `geofence_check` ✔ | point or path against the India–Sri Lanka IMBL and protected areas |
| `route_grid` | traversal-cost grid from bathymetry, land mask and geofences |

### 3.6 Risk agent — `agents/risk_agent.py`

**Owns:** the verdict, and the route.

| tool | what it does |
|---|---|
| `compute_risk_score` ✔ | **the verdict comes from here and nowhere else.** Reads wave height, wind speed, visibility, steepness and the alert count; compares each against the threshold for that vessel class; returns `go` / `caution` / `no_go` plus which driver caused the downgrade |
| `optimise_route` | A* over the traversal grid, avoiding land, shallows and geofenced water |

Thresholds live in `config/risk_thresholds.yaml`, **with a citation comment per
threshold**, traceable to INCOIS small-vessel alert criteria. That file is a
deliverable, not configuration — it is what you open when a judge asks "why
2.5 m?".

### 3.7 Synthesis agent — `agents/synthesis_agent.py`

**Purpose.** Assemble the typed fragments the agents returned into one
`Recommendation` object, then — after verification — narrate it.

Agents return **typed fragments, never prose**. A claim is stored as
`{"swell_m": 2.8, "dir": "SE"}` plus a template, never as a finished English
sentence, because translating a finished safety sentence corrupts the numbers
inside it.

### 3.8 Verifier — `orchestrator/verifier.py`

**Not an agent and not an LLM.** A deterministic function that walks every claim
in the recommendation, finds each number, and confirms it appears in the output
of the tool call the claim points at.

It walks an **independent tool-call log**, never the answer's own evidence block
— checking a document against itself proves nothing.

It is stricter for `observed` claims than for `inferred` ones. There is a test
that feeds it a hallucinated number and asserts rejection.

It has already caught a real error: synthesis wrote `abs(anomaly_sigma)` where
the sign mattered, and the verifier refused the answer rather than showing it.

---

## 4. Which agents use an LLM, and for what

| agent | LLM? | what the model actually does |
|---|---|---|
| router | ✔ | decides conversation vs. query; writes the conversational reply |
| intent + planner | ✔ | classifies the question, fills slots, emits the tool DAG |
| ocean | ✔ | reads its own findings and says what is missing or suspicious |
| weather | ✔ | same, for its own findings |
| geospatial | ✔ | same |
| risk | ✔ | same |
| hypothesis proposer | ✔ | proposes causal explanations to be **tested by code** |
| narrator | ✔ | turns a verified object into English |
| synthesis | ✘ | assembles typed fragments; deterministic |
| verifier | ✘ | checks numbers; deterministic |

Six of seven agents reason with a model. **None of them produces a number.**

---

## 5. Where the data comes from

### 5.1 The cache-first rule

> **Nothing is fetched during a user query.**

`ingest/` populates a local cache on a schedule. Tools read the cache. A tool
that had to reach the internet would make answer latency depend on someone
else's uptime, and would fail in the middle of a demo on venue wifi.

Run the ingest by hand with:

```bash
python scripts/refresh_cache.py --weather --ocean --alerts --static
```

### 5.2 Sources

| what | source | keyless | notes |
|---|---|---|---|
| waves, swell, wind-wave | Open-Meteo Marine `marine-api.open-meteo.com/v1/marine` | ✔ | also `sea_level_height_msl`, our tide signal |
| wind, gusts, visibility | Open-Meteo `api.open-meteo.com/v1/forecast` | ✔ | |
| historical replay | Open-Meteo Archive `archive-api.open-meteo.com/v1/archive` | ✔ | Cyclone Fengal, Mandous, Gaja |
| sea-surface temperature | NOAA CoastWatch ERDDAP, `jplMURSST41` | ✔ | 0.01°, strided to the target grid |
| chlorophyll-a (daily) | NOAA CoastWatch, `nesdisVHNnoaaSNPPnoaa20NRTchlaGapfilledDaily` | ✔ | **gap-filled** — monsoon Coromandel is overcast for days and an L2 product would be full of holes during a demo |
| chlorophyll climatology | `nesdisVHNSQchlaMonthly` | ✔ | ≥5 years reduced to per-pixel per-month mean and σ |
| cyclone advisories | GDACS `gdacs.org/gdacsapi/.../geteventlist` | ✔ | normalised to `{type, severity, zone, issued_at, valid_until, authority, text}` |
| bathymetry | SRTM30_PLUS | ✔ | `z` is elevation, so depth is `-z` |
| IMBL / maritime boundary | UN treaty texts, LKA–IND 1974 and 1976 | ✔ | digitised from the treaty turning points, not a shapefile |
| landing centres | gazetteer in `config/bbox.yaml` | | 20 points (7 seed + 13 verified 2026-09-07, per-point source/confidence); INCOIS set still wanted, caveat kept |

The Copernicus Marine dependency named in the original plan was replaced by NOAA
CoastWatch, which is keyless and has never rate-limited us.

### 5.3 The target grid

Everything is regridded at ingest onto **0.05°, EPSG:4326**, 81 lats × 71 lons,
covering lon 78.5–82.0, lat 8.0–12.0.

Every gridded field carries `data_age_days` and `clear_pass_fraction` alongside
it, and those surface in `confidence.basis` as, for example, *"7-day composite,
last clear pass 3 days ago"*. **Stale data is never silently substituted for
fresh.**

### 5.4 How data reaches the LLM — and how it does not

This is the part people usually get wrong, so it is worth being precise.

**The model never sees a grid.** It never receives an array of SST values, a
NetCDF slice, or a raw forecast series. What it receives is:

*At planning time* — the question, the conversation so far, and a **compact tool
catalogue**: tool names, argument names and types. Not data. The catalogue is
generated from the registry programmatically (`registry.compact_tool_block()`),
never hand-maintained, because a hand-written tool list drifts within a week.

*At deliberation time* — its own agent's **typed findings**, already computed:
`{"front_count": 3, "max_gradient_c_per_km": 0.18, "chl_anomaly_sigma": -1.4}`.
Small, structured, and already true. The model reads these and says, in words,
what is missing or suspicious. Its reply goes through `strip_numbers()`, so it
can say *"the zone lies far offshore for this boat"* but cannot say *"the zone
lies 47 km offshore"*.

*At narration time* — the **verified** `Recommendation` object, and only after
`verify()` has passed. The narrator's job is to write English around numbers that
are already proven; a guard then compares its prose against the object's own
numbers and falls back to the deterministic template renderer if it drifted.

So the data path is: **network → cache → typed tool output → tool-call log →
verified claim → prose.** The model touches the last two steps and the first
decision. It never touches a measurement.

---

## 6. How the agents communicate

### 6.1 Not a chat room

Agents do not send each other free text. Free-text negotiation between models is
where hallucinations compound, and there is no way to verify a paragraph one
model wrote to another.

They communicate by **requesting tool calls**, which are validated exactly like
the planner's, executed by the same executor, and logged in the same tool-call
log. `orchestrator/collaborate.py`.

### 6.2 The loop

```
round 1   plan executes            → each agent gets its own typed findings
          ↓
          deliberate()             → one LLM call per agent that has findings
          ↓                          "what is missing? what looks wrong?"
          returns: assessment (prose, numbers stripped)
                   concerns   (prose, numbers stripped → become caveats)
                   requests   (STRUCTURED: tool name + arguments)
          ↓
          each request is validated against the registry
          ↓
round 2   accepted requests execute → findings merge into the same result
```

`MAX_ROUNDS = 2`, `MAX_ADDED_STEPS = 4`. Bounded on purpose: an unbounded agent
loop is a way to burn a rate limit in the middle of a demo.

### 6.3 What a request must survive

| check | why |
|---|---|
| the tool exists in the registry | an agent cannot invent capability |
| arguments type-check against the Pydantic input model | same gate the planner passes |
| `optional` is read from the **registry**, not from the requester | an agent declaring its own request non-critical was auto-rejecting every safety-critical tool. Real bug, found live |
| a bad request does not sink the round | the round retries with the rule floor alone, so one malformed request cannot cost the turn |

### 6.4 The causal loop is different, and stricter

For `causal_explain`, the model **proposes hypotheses** — "productivity is
unusually low", "a bloom or turbidity is masking the signal", "the frontal
structure is weak this month".

Each proposal must map onto a **test that code can run** (`agents/hypotheses.py`,
the `TESTS` table). Then:

- tested and supported → reported as supported
- tested and refuted → **reported as refuted**, which is a real finding
- cannot be tested → **dropped silently, and the answer says some were dropped**

An untested hypothesis is never shown. That is the difference between an
explanation and a guess.

---

## 7. What the user sees, and why

| element | meaning |
|---|---|
| verdict chip | `go` / `caution` / `no_go`, from `compute_risk_score` alone |
| drivers | each with observed value **vs** threshold, so the reason is legible |
| negative findings | "no cyclone advisory in force" — asserted, having checked |
| `✓ N numbers verified` | the verifier matched N numbers to tool outputs |
| `degraded` | a tool failed or the data is stale; the answer is partial and says so |
| suggestion buttons | written by the model per turn. If you see the same four with place names baked in, the offline fallback is running and something is wrong |
| evidence drawer | every claim, its `kind`, and the `tool_call_id` it came from |

`kind` is on every claim and the verifier treats the three differently:

- **observed** — a chlorophyll reading. Must match a tool output exactly.
- **derived** — "moderate sea state". Produced by a named rule.
- **inferred** — "skipjack may aggregate here". Never carries a hard number.

---

## 8. Degradation, honestly

The system has five tiers, and each one says which it used:

1. **Groq LLM** — planner, per-agent deliberation, narration
2. **Conversational retry** — planner schema failed; model asked again without it
3. **Previous turn's context** — slots carry, query type is re-read from this
   turn's words
4. **Keyword classification** — proposes a query type only; slot gates still fire
5. **No model at all** — deterministic template renderer, and the reply says
   plainly that the language model is unreachable

The narrator falls back to `language/templates/en`, which is deterministic and
always correct — just less fluent. **The system never loses the ability to
answer.**

---

## 9. Layout

```
config/         bbox, datasets, risk_thresholds (cited), models
core/schemas/   recommendation, intent, plan, tool_io   ← imports nothing
core/           provenance (tool_call_id logging), units, env
ingest/         sources/ static/ transform/ catalogue climatology replay
tools/          registry + ocean, weather, geo, risk
agents/         one file per agent + prompts/*.md
orchestrator/   main (FastAPI), turn, executor, collaborate, session,
                validate_plan, verifier, llm/
language/       detect (returns "en"), translate (identity), templates/en
frontend/src/   components, api, hooks, types
data/           gitignored: zarr, raw, static, replay
```

Prompts live in `agents/prompts/*.md`, never as inline strings. Frontend types
are generated from the Pydantic schemas (`scripts/gen_types.py`), never
hand-written, because hand-written types drift.

---

## 10. Running it

```bash
python scripts/refresh_cache.py --weather --ocean --alerts --static
python -m uvicorn orchestrator.main:app --port 8000
cd frontend && npm run dev
```

Replay a real cyclone rather than demoing on a calm day:

```bash
python scripts/replay.py --event fengal
```

A live demo on a calm day proves nothing. Watching the risk score go red
thirty-six hours before landfall proves everything.

---

## 11. Known limits

Stated plainly, because a system that hides its edges cannot be trusted at the
ones it shows.

- **Tamil and regional languages** are not implemented. The language adapter is a
  pass-through stub so it is a one-adapter change, and it is deliberately not
  deleted.
- **MPA and EEZ geometry** is incomplete. "Clear of boundaries" is currently
  qualified to the IMBL alone, and the answer says so.
- **The landing-centre gazetteer is provisional** — 20 points (7 seed + 13 verified against OSM Nominatim, each with source and confidence below 1.0), not the INCOIS set.
  Every answer that resolves a place carries a caveat saying to confirm the
  coordinate against the official database before navigating.
- **Ingest is run by hand.** `scripts/refresh_cache.py` is not yet scheduled.
- **No lightning feed** — no public source found.
