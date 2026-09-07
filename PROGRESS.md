# ORCA — Progress

Living status document. Updated as work lands, not as it is planned.

**Last updated:** 2026-09-07 · **Tests:** 386 passing, 15 skipped, 0 failed (venv, weather+alert cache, hermetic) · **Head:** Phase 4 + streams 0-4, 6 + UI reskin + /settings · **Tools:** 14/14 · **LLM:** opencode Zen first, then Groq

---

## What landed on 2026-09-07 (later) — UI reskin + real settings endpoint

Frontend rebuilt around the `ORCAui` hydrographic-chart reference (light
sea-glass default, dark abyss toggle, Newsreader/Archivo/Plex Mono, rail +
topbar + bridge/thread + side panel), re-grounded to OUR coast: Coromandel
box, Nagapattinam/Cuddalore/Rameswaram copy, IMBL+MPA honesty. Mock-only
fixtures (Mangaluru, AIS counts, lightning radar, account/upgrade, random
sounding numbers, selectable model menu) were cut or replaced with live
backends; the verdict banner, `✓ N verified` meta, options, collaboration,
and evidence trail are new UI surfaces for existing API fields.

- **All UI copy in `frontend/src/i18n/strings.ts`.** One object, `fill()`
  slot helper mirroring claim templates. Tamil later is a second object +
  a switch -- the same one-adapter shape as the backend language stub.
- **Rail is all-real:** Bridge, New query, Conversations (GET
  `/session/{id}` + localStorage recents), Saved zones (localStorage,
  keeps the recommendation so evidence/map reopen), Alerts (badge = real
  `in_force` count, `!` while unchecked), Settings.
- **Settings sheet, all knobs live:** tier Free/Fast/Paid via new
  `GET/POST /settings` (runtime override, next question uses it, `null`
  returns authority to `ORCA_TIER`; 5 tests in `tests/test_settings.py`),
  Light/Dark (localStorage, default light), English + disabled Tamil row,
  data status from `/readiness` layers.
- 4th task card is **Sea conditions** (fishermen check it daily;
  `causal_explain` lives in chips+suggestions, safest-route inside safety
  answers). Map stays MapLibre with real geometry; readout shows only
  `spatial_context` coords, never cursor-depth fiction.
- Mobile from the start: 1120/1000/820 breakpoints, icon rail, scrollable
  chips, scrollable tables, `tsc -b` + `vite build` green.
- Not visually verified in a browser here -- fonts and OSM tiles need
  network at runtime like before.

## What landed on 2026-09-07 (later) — honesty batch, mobile shell, screenshot harness

Two-agent UI audit (flow recon + gap critiquer) returned real findings;
the honest-and-crash batch is built, verified with headless screenshots
(Playwright/Chromium via `/tmp/pw` venv, `/tmp/shot.py`; Firefox headless
can't wait for JS). Desktop + 390px shots read clean, zero console errors.

- **Stale side panel fixed:** turns with no recommendation (or no origin)
  close the panel instead of framing the previous answer's map/evidence.
- **Boundaries everywhere it matters:** side panel wrapped (Sheets
  already was); MapView removes the map on unmount (was leaking a WebGL
  context per open/close).
- **Busy affordances:** bridge cards/chips and answer options dim and
  hold while a question flies; mid-flight New query invalidates the stale
  response by request id; `DELETE /session` is actually called on session
  rotate; API errors go to the console, the user gets one sentence.
- **Copy honesty:** feed pill says "weather cached/missing" (ready gates
  on weather only), evidence footer no longer claims failed answers are
  never shown, alerts count is all advisories not cyclones, tier drops the
  "3 s" timing claim.
- **Dead code deleted:** `isSaved`, Composer `initial`, unreferenced
  string keys and CSS confirmed by grep.
- **Mobile shell:** hamburger drawer rail under 820px, sticky
  screen-bottom thread composer, compact fading chips, two-row topbar,
  bridge kbd hint hidden on phones. Verified at 390×844.
- **Still open:** route legs on the map (waypoints never reach the
  recommendation), thread-title drift, focus restore on sheet close,
  settings reset-all button (API supports `reset`, no UI), vis_km column
  in replay table, Tamil, MPA/EEZ geometry, INCOIS high-wave feed,
  scheduled ingest, satellite cache (SST/chlorophyll still missing -
  CoastWatch SSL fails from here, PFZ/causal stay degraded).

## What landed on 2026-09-07 (later) — replay critique fixes

The replay critiquer returned revise with one high-severity item and
it was right: the swap hazard was prose-only. Now `/chat` and
`/chat/stream` 503 (Retry-After 60) while a replay owns the cache --
loud refusal instead of poisoned answers, covered by test. Also fixed:
`first_breach` derives from the breach list (never the display string),
offset-aware `landfall`, warning field in the payload, `step_hours`
422s out of range, verdict as `str` (a new band degrades, never 500s),
lock release inside `finally`, busy-then-success + FAILED-nulls +
summary-None + exact-409 tests. Suite: 423 passed / 11 skipped (the
Fengal archive un-skipped four). Left as documented: multi-worker
deployments need single-worker constraint (stated in code + payload).

## What landed on 2026-09-07 (later) — critique fixes, both workers

Two glm critiquers reviewed the workers' output; every confirmed item
is fixed and live-verified: replay warning + note rendered, IST-pinned
times (Asia/Kolkata, year included), summary shape guard, focus-in +
full trap in both sheets (12/12 Tabs stay inside, Escape closes),
keyboard-scrollable replay table region, clamped overall meter. The
replay trajectory screenshot shows it all working.

## What landed on 2026-09-07 (later) — replay endpoint

`POST /replay/{event_id}` runs an archived cyclone through the
production tools and returns the verdict trajectory. Same row-building
as `scripts/replay.py` (gust peak, breaching drivers first), guarded by
a process-global lock with 409-on-busy, 404 unknown, 409 + fetch hint
when the archive is missing. 5 tests (row rules against the real risk
function, lead-time math, lock/404/409 paths). Fengal archive fetched
(120 h/layer) and driven live: `no_go` 71.5 h before landfall,
recovering after passage. Suite: 413 passed / 15 skipped.

## What landed on 2026-09-07 (later) — live pipeline trace (SSE)

`POST /chat/stream` runs the identical `run_turn()` in a worker thread
and yields stage frames from a per-turn `ProgressBus`
(`orchestrator/progress.py`): plan → execute → deliberate → collaborate →
synthesise → verify → narrate, then the full `ChatResponse` as `done`.
The UI renders a live trace with per-agent rows and falls back to
blocking `POST /chat` on any transport trouble. Settled design point:
stage events stream, prose never streams pre-guard -- narration appears
via a post-guard reveal, so the verify-before-narrate order is
untouched. `tests/test_stream.py` pins it: allowlist-only stages in
order, no fabricated fields, stream==blocking answer, concurrent turns
both terminate, verify-fail emits no narrate. Suite: 405 passed /
15 skipped. Verified live in-browser: trace rows appear mid-turn.
Also shipped: pointer/typing-reactive composer halo + backdrop parallax
(rAF-throttled custom props, composite-only, reduced-motion and mobile
kill-switches), shared `_to_response` builder so both routes agree.

## What landed on 2026-09-07 (later) — treaty line on the chart

The audit's top map complaint is fixed without touching the verdict
path: `GET /geo/boundaries` serves the IMBL polyline from the same
digitised treaty source the geofence tool tests (`imbl_linestring()`),
so the drawn line and the checked line cannot drift apart. MapView draws
it red-dashed with a legend row that only appears when the fetch
succeeds; a failed fetch leaves an honest boundary-less map. 3 tests
(positions match source, line crosses the box, provenance travels).
Suite: 401 passed / 15 skipped. Verified with a live geofence answer:
Rameswaram origin + treaty line on the sector chart.

Route legs stay un-drawn on purpose: `optimise_route` waypoints never
reach the recommendation (no waypoint claims in synthesis), and piping
them through is a synthesis change for another pass.

## What landed on 2026-09-07 (later) — worker knob batch + UI surfacing

A worker (`general.worker`, tight file allowlist, backend only) added 7
runtime knobs on the override pattern: context/pending TTLs, max rounds
+ added steps, template fallback, LLM + step timeouts. 12 tests pass in
`tests/test_settings.py`, ruff shows only the 6 pre-existing hits
(verified identical at HEAD). I reviewed the diff, restarted the API,
and surfaced the three user-meaningful knobs in the sheet (memory
presets, safety-net toggle, follow-up checks) with preset buttons so no
422 is reachable from the UI. Round-trip + 422 verified live;
settings-sheet screenshot verified. Pending/added-steps/LLM/step
timeouts are API-live but UI-unsurfaced by choice (operator-grade).

## What landed on 2026-09-07 (later) — subagent-diagnosed settings rows

A vision subagent (explorer.logic on muse-spark-1.3-contributor; the
default deepseek child has no vision) read a settings screenshot and
traced four defects to `Sheets.tsx:280-285`: stray leading `·` on the
alerts row (separator rendered with no preceding fragment), empty
sub-lines on uncached ocean rows, raw backend keys as labels, 2dp ages.
Fixed as one joined sub-line rendered only when non-empty, i18n labels
with raw-key fallback, render-side rounding. DOM + screenshot verified.
Spelling tiebreaker settled: backend sends `chlorophyll` correctly.

Ops note, twice bitten: vite dev serves a poisoned transform after
mid-edit HMR ("does not provide an export named 'default'" with a clean
`tsc`). Fix is `pkill -9 -f "[b]in/vite"` (brackets keep pkill from
matching its own command line) and restart with `--force`. Never bare
`pkill -f` a pattern that appears in the invoking command.

## What landed on 2026-09-07 (later) — deliberation toggle + busy honesty

Chips "only work once" because answers take so long users bail
mid-flight while `busy` swallows clicks. Two fixes: the bridge now shows
a live status line while an answer flies, and agents' LLM fan-out can be
switched off. `POST /settings {deliberating:false}` sets a process-local
override (same pattern as the tier switch; restart returns to on) gated
next to the existing `deliberating` flag, so rule-only answers stay
correct. 3 new tests in `tests/test_settings.py` (11 total there); ruff
clean; endpoint verified live including round-trip.

---

## What landed on 2026-09-07 (night) — stacked hardening: Zen, conditions, geo-data, RAG, green suite

Five stacked PRs (404Mayank/ORCA, #1-#5), each reviewed against a critiquer before merging, plus this test-env stream:

- **Repo surgery.** The tree lived under `ORCA-main/` in the index with the real project untracked. Moved to root, real `.gitignore`, editable install fixed (explicit package list), `.env.example` restored + `OPENCODE_API_KEY`.
- **OpenCode Zen provider** (`orchestrator/llm/client.py`, first in `provider_order`): chat/completions only, missing-key fast-fail with no socket call, 429 falls through, never raises. Role split planner kimi-k2.6 / deliberator deepseek-v4-flash / narrator kimi-k2.5, ids verified against the live roster. No key in this shell, so no live inference check yet.
- **`conditions_report`.** Read-only tide/weather/alert answers, no verdict, place-only. The fence is code on both tiers: safety-decisive language (one weight>=2 safety term) routes to `safety_assess` at any margin; validator forbids `compute_risk_score`; renderer prints observed claims only on verdict-less answers (safety text unchanged). 15 tests.
- **Geo-data.** Gazetteer 7 -> 20 points, each new coordinate independently verified against OSM Nominatim (drifts and single-source caveats recorded, confidence 0.85/0.9, never 1.0). Dataset registry populated; INCOIS loader + catalogue lookup implemented. MPA polygon attempted via the WDPA mirror and honestly NOT committed (absent from the snapshot); re-runnable checker script kept; geofence still reports mpa/eez unavailable.
- **Gated RAG.** PostgREST FTS over httpx (zero new deps), disabled-by-default, fail-closed; strip_numbers barrier in code; corpus from cited in-repo text only; propose() TESTS gate unchanged.
- **Suite honesty.** The claimed "347 passing" did not collect here (missing geo deps + 21 router-drifted planner tests + live-cache-dependent synthesis tests). Repaired: router-aware stubs, Gate-0-removal updates, live-cache skip idiom (`succeeded()` not `is not None`), real weather+alert cache via refresh. Now **376 passed, 15 skipped, 0 failed**; skips are satellite-cache-dependent only. Ruff: new/changed files clean; 185 pre-existing repo-wide violations left alone.

Deferred explicitly: Tamil/i18n (AI first, owner call), Supabase cloud provisioning, docker-compose (no pg driver by design).

## What landed on 2026-09-07 (later) — the conversation belongs to the model

The morning's `chat` state fixed the loop but replaced it with a subtler
version of the same mistake: every turn the system could not place became a
**fixed paragraph**, chosen by a hardcoded word list. One live session made the
problem impossible to miss.

```
hello   ->    0 ms   canned paragraph, identical every time
alloo   -> 1559 ms   "Hello! How can I help you with your fishing plans today?"
```

Same kind of turn. The good reply is the model's; "alloo" only got it because
it was **missing from the word list** and fell through. That is the whole
argument against the design, produced by the design itself.

Worse, three turns in that session -- including **"what is safest route for my
vessel"** and **"fishing zones ??"**, both squarely in scope -- came back with
the same canned paragraph. Their latencies gave it away: ~4500 ms against
~1500 ms for the turns that worked, i.e. call, parse failure, retry, parse
failure, give up. The model had understood them. A schema error threw its
answers away.

### What changed

| | |
|---|---|
| Gate 0 | no longer answers greetings. It withholds `carry_these_slots` -- the part that was actually load-bearing -- and lets the model reply |
| `_chat_via_llm` | when the planner's JSON cannot be parsed twice, the model is asked again **with the schema removed**. It is demonstrably up; only the shape was unusable |
| `parse_planner_json` | a `suggestions` list longer than four is **trimmed**, not rejected. A layout limit must not fail like a safety limit |
| `SessionStore.conversation()` | real dialogue -- what was asked and what we said back -- reaches the planner. Verdicts still withheld, so it cannot repeat one |
| `SessionStore.pending_question()` | looks **past** chat turns. Small talk answers nothing, so it cannot cancel a question we asked |
| offline message | says "I cannot reach my language model", because that is now the only case that reaches it |

`CAPABILITIES` and `SUGGESTIONS` survive as the no-provider tier only. The
keyword classifier stays where CLAUDE.md always put it: below Ollama, reached
only when nothing else can run.

### What did not change

Every number, and every verdict. `strip_numbers()` still runs on every chat
reply and still fails loudly, because that guard is about truth rather than
about fitting on screen. The model chooses what to look up and how to say it;
code decides what is true. The last turn of that session printed **"15 numbers
verified"**, and that sentence is only true because code produced all fifteen.

## What landed on 2026-09-07 (earlier) — the system can hold a conversation

The planner had **three** states: `plan`, `clarification`, `refusal`. There was
no way for ORCA to simply *talk*. Every turn that was not one of the four marine
queries fell through to the safety slot gate, so:

* "hello" was answered with *"Which landing centre are you leaving from, and
  what kind of boat is it?"*
* answering it half-way ("kattumaram") asked the other half
* typing anything unrecognised ("alloo") restarted the whole question

The user could only ever click a sample query. Reported with a screenshot.

**The fix is a fourth state, `chat`.** The planner may now decide that a turn
needs no tools at all and reply in its own voice, with up to four suggested
questions the UI renders as buttons.

| | |
|---|---|
| `core/schemas/intent.py` | `ChatReply`; `PlannerOutput.state` gains `"chat"`, still exactly-one-of |
| `agents/prompts/planner.md` | when to chat vs. plan vs. refuse; a chat reply may contain no numbers |
| `agents/intent_planner_agent.py` | `_chat()` applies `strip_numbers`; Gate 0 answers greetings locally; Gate 0b returns a chat turn before inheritance or the slot gate |
| `orchestrator/turn.py` | `state == "chat"` returns the reply, suggestions in `options` |
| `frontend/` | `.options.suggest` — sentence-case buttons, because uppercase monospace makes a sentence look like an error code |

Three things worth noting about how it is wired:

* **A greeting costs no LLM call.** Gate 0 catches it and answers from
  `CAPABILITIES`. `attempts == 0` is asserted in a test.
* **A chat reply is number-stripped.** It calls no tool, so it reaches no
  evidence block, so any figure in it is unverifiable by construction. The
  governing rule reaches the conversational path too.
* **The no-LLM tier chats as well.** With both Groq keys rate limited and no
  keyword match, the honest answer is "I did not catch what you need" plus what
  we can do — not an interrogation about a safety question nobody asked.

Two things that bit on the way, both now covered by tests:

* **The model returned `query_type: null` on a chat turn** — correct, since a
  greeting is none of the four types, and unrepresentable, since a required enum
  has no "none of these" member. The parse failed, the retry failed identically,
  and a question the model had understood perfectly came back as "I did not
  catch what you need". The parser now fills a placeholder **on the chat state
  only**; every other state still rejects a null loudly.
* **Groq's free tier meters tokens per day per model, per _organization_ — not
  per key.** Three keys from one account reported the same org and the same
  exhausted 200k pool. Extra keys spread burst rate; they do not buy a second
  day's tokens. A key from a different account does. Four are configured.

Chat turns are deliberately **not** inheritable: they fill no slots, so a
follow-up cannot pick up a place or a boat from one.

## What landed on 2026-09-06

The project arrived at this session with 9 of 14 tools built, 2 of 4 query
types answerable, 96 of a claimed 215 tests actually runnable, no API, no
frontend, and every safety verdict pinned to `no_go`. What changed:

**Data — the two blockers dissolved**

- The **Copernicus Marine** account is no longer needed. CLAUDE.md asked for
  gap-filled L4 data, not for Copernicus specifically, and NOAA CoastWatch
  serves it keyless: MUR SST at 0.01° (finer than the Copernicus product the
  brief assumed) and DINEOF gap-filled VIIRS chlorophyll.
- The **alert source** exists. GDACS (EU JRC + UN OCHA) carries Bay of Bengal
  cyclones, keyless. IMD's own APIs are authenticated (HTTP 401). High-wave and
  swell-surge have no feed at all and come from an operator table.
- `refresh_points()` had been written and **never called by anything**, so a
  fresh checkout had no way to fill its cache. `scripts/refresh_cache.py` is
  that runner.
- Bathymetry (SRTM30_PLUS), a 13-year VIIRS chlorophyll climatology, and a
  cyclone replay archive.

**Reasoning — from two LLM calls to six agents that think**

- The three ocean tools, then `route_grid` and `optimise_route`: 9/14 → **14/14**.
- Four domain agents written, then given their own LLM deliberation.
- Inter-agent collaboration: agents review results and extend the plan.
- LLM-proposed causal hypotheses, tested in code, untestable ones dropped.
- A keyword tier below Ollama so the system answers with no key at all.

**Delivery**

- FastAPI (`/chat`, `/health`, `/readiness`, `/session`), one shared turn
  pipeline for HTTP and CLI, multi-turn sessions.
- React + Vite + MapLibre frontend with an explainability drawer; TypeScript
  types generated from the Pydantic schemas, staleness pinned by a test.
- Cyclone replay through the production tools.

**Tests: 96 runnable → 339 passing.**

---

## Running it

```bash
pip install -e ".[geo,dev]"
python scripts/refresh_cache.py     # weather, satellite, alerts, bathymetry
python -m pytest -q                 # 339 passing

uvicorn orchestrator.main:app       # API  → http://127.0.0.1:8000/docs
cd frontend && npm install && npm run dev   # UI → http://localhost:5173
```

Demo scripts:

| Script | Shows |
|---|---|
| `scripts/replay.py` | a real cyclone turning the verdict red, 72 h out |
| `scripts/try_agents.py` | all four query types, agent fragments, no LLM |
| `scripts/try_planner.py "<question>"` | the live planner building a DAG |
| `scripts/try_narration.py` | the number guard on real prose |

`.env` needs `GROQ_API_KEY` (and optionally `GROQ_API_KEY_2` — six calls a turn
rate-limits one free key). Without any key the system still answers.

---

## Where we are in one line

**All seven agents exist, all four query types run end to end on live data, and
every agent reasons for itself.**

A free-text question goes LLM plan → validate → execute → **agents review and
extend the plan** → synthesise → **verify** → LLM narrate → English.

**Six of the seven agents use an LLM.** Only the verifier does not, and it never
will. What an agent may decide is *what to do*: which tools to run, what it is
still missing, which explanations are worth testing. Every number still comes
from a tool and is checked against the tool call log before anyone sees it.

The system answers with **no LLM at all** — keyword intent classification,
hardcoded fallback plans, rule-based collaboration and template rendering — so
the venue losing internet costs fluency, not function.

---

## Agent status

| Agent | Tools | Uses an LLM | Status |
|---|---|---|---|
| `intent_planner_agent` | n/a | ✅ plans | ✅ Complete — live via Groq |
| `weather_agent` | 4 / 4 | ✅ deliberates | ✅ Complete — alerts live via GDACS |
| `geospatial_agent` | 5 / 5 | ✅ deliberates | ✅ Complete |
| `ocean_agent` | 3 / 3 | ✅ deliberates | ✅ Complete — live on NOAA CoastWatch |
| `risk_agent` | 2 / 2 | ✅ deliberates | ✅ Complete |
| `synthesis_agent` | n/a | ✅ narrates + proposes hypotheses | ✅ Complete — all four query types |
| verifier | n/a | ❌ **never** | ✅ Complete |
| executor | n/a | ❌ | ✅ Complete |

A domain agent's LLM call decides *what else to look at* and *what to warn
about*. It cannot decide what is true, cannot write a number, and cannot mark
its own request safety-critical. See **Every agent reasons** below.

## Query status

| Query | Runs end to end | Notes |
|---|---|---|
| `geofence_check` | ✅ Yes | IMBL only; MPA and EEZ geometry not obtained |
| `safety_assess` | ✅ Yes | **Reaches `go`** — cyclone alerts live via GDACS |
| `pfz_locate` | ✅ Yes | Live SST fronts + chlorophyll; September is flat, so few candidates |
| `causal_explain` | ✅ Yes | Chlorophyll anomaly against a 13-year VIIRS baseline |

---

## What is blocking us

1. **INCOIS high-wave / swell-surge feed.** No machine-readable source exists;
   IMD's own APIs are authenticated (HTTP 401). Covered by the operator table
   in `config/active_alerts.yaml`, which a human edits before a shift. Cyclones
   are live via GDACS, so this no longer blocks a `go` verdict — it caps how
   much of the advisory picture is automatic.
2. **Vessel cruise speeds, beam widths, operating ranges.** Still `provisional`
   and load-bearing: they set the turn-back time and cap PFZ range.
3. **INCOIS landing centre points.** 7-point provisional seed; `resolve_place`
   reports 0.8 confidence because of it.
4. **MPA / EEZ geometry.** Only the IMBL has geometry, so "clear" is always
   qualified. Bathymetry is now wired (SRTM30_PLUS), so routing is unblocked.
5. **SVAS technical note.** Would replace the provisional threshold block.

**No longer blocking:** Copernicus Marine account; the alert source for cyclones.

## Phase 0 — the frozen core ✅ COMPLETE

| Item | Status |
|---|---|
| Ideal answer written by hand first | ✅ `docs/ideal_answers/safety_assess.md` |
| `core/schemas/` frozen against it | ✅ |
| `core/units.py` — Range, Threshold, evaluate | ✅ |
| `config/bbox.yaml` | ✅ |
| `config/risk_thresholds.yaml` with citations | ✅ |
| `tools/registry.py` | ✅ 14 tools |
| `orchestrator/validate_plan.py` | ✅ + fallback plans for all 4 queries |
| `orchestrator/verifier.py` | ✅ **incl. hallucination rejection test** |
| `fixtures/` populated | ✅ ideal, degraded, refusal variants |

## Phase 1 — deterministic tools ✅ COMPLETE (14/14)

| Tool | Agent | Status |
|---|---|---|
| `compute_risk_score` | risk | ✅ |
| `resolve_place` | geospatial | ✅ provisional gazetteer |
| `distance_bearing` | geospatial | ✅ geodesic WGS84 |
| `nearest_landing_centre` | geospatial | ✅ |
| `geofence_check` | geospatial | ✅ IMBL from treaty text |
| `wave_forecast` | weather | ✅ live Open-Meteo |
| `wind_forecast` | weather | ✅ live Open-Meteo |
| `tides` | weather | ✅ `sea_level_height_msl` |
| `active_alerts` | weather | ✅ **fails honestly**, no source wired |
| `route_grid` | geospatial | ✅ SRTM30_PLUS bathymetry + geofence |
| `optimise_route` | risk | ✅ A* over the cost grid |
| `thermal_front` | ocean | ✅ live MUR SST, gradient + clustering |
| `chl_anomaly` | ocean | ✅ live VIIRS + 13-year monthly baseline |
| `pfz_candidates` | ocean | ✅ derived from front + chlorophyll |

Also done: `orchestrator/executor.py`, `core/provenance.py`,
`agents/synthesis_agent.py`, `language/` adapter + English templates,
`ingest/sources/open_meteo.py`, `ingest/static/boundaries.py`,
`ingest/sources/erddap.py`, `ingest/climatology.py`,
`scripts/refresh_cache.py` — **the ingest runner that was missing**.

## Phase 2 — real data 🟢 IN PROGRESS (satellite done, geometry outstanding)

| Item | Status |
|---|---|
| Satellite SST | ✅ MUR L4, 0.01°, NOAA CoastWatch, keyless |
| Satellite chlorophyll | ✅ VIIRS DINEOF gap-filled, keyless |
| Climatology reduction | ✅ `ingest/climatology.py`, per-pixel per-month mean/std |
| Ingest runner | ✅ `scripts/refresh_cache.py`, idempotent |
| Bathymetry | ✅ SRTM30_PLUS ~900 m, keyless |
| Coastline, MPA, EEZ geometry | ⬜ |
| Zarr store, Supabase schema | ⬜ cache is JSON on disk; adequate for now |
| Gaja replay dataset | ⬜ `fetch_archive()` exists, unused |

## Phase 3 — the agents ✅ CORE DONE

| Item | Status |
|---|---|
| LLM client, tiered (groq → anthropic → ollama) | ✅ `orchestrator/llm/client.py` |
| Prompts as `.md`, tool block injected from registry | ✅ `agents/prompts/` |
| intent_planner_agent — one call, two blocks, code gates | ✅ live-verified |
| Guarded LLM narration | ✅ live-verified |
| Ollama offline tier | ⬜ backend written, not exercised |
| Groq tier | ✅ **live**, `openai/gpt-oss-120b`, planner + narrator |
| Multi-turn session state | ✅ live-verified 2026-09-06; slots carried **and recorded** |
| Clarification answering | ✅ a reply fills the slot it answered; the next question asks only for what is still missing |
| Conversational follow-ups | ✅ slots carry and update; the query type is re-read every turn, with or without an LLM |
| Four domain agents (ocean, weather, geospatial, risk) | ✅ `agents/base.py` + one file each, deterministic |
| Plan composition from agents | ✅ `compose_plan()`; all four query types validate |
| **Inter-agent collaboration** | ✅ `orchestrator/collaborate.py` — review → request → extend → re-execute |
| **Per-agent LLM deliberation** | ✅ `agents/deliberate.py` + a prompt per domain agent |
| Agent concerns folded into caveats before verification | ✅ numbers stripped at the source |
| **LLM-proposed causal hypotheses** | ✅ `agents/hypotheses.py` — proposes, code tests, untestable ones dropped |
| Collaboration surfaced in the API and UI | ✅ `collaboration[]` on `/chat`; rendered inline |

**Planner gates, all enforced in code regardless of what the model says:**
a safety question missing location or vessel class → clarification; invalid
plan → one replan with errors fed back → hardcoded fallback → honest
`no_data` refusal if even that cannot run. Live-checked on 2026-09-04:
a fully specified question planned first try with no fallback; a question
with no place or boat was asked back; a `pfz_locate` question was refused
because the ocean tools are not connected.

## Phase 4 — API ✅ / frontend ✅ / replay ✅

| Item | Status |
|---|---|
| FastAPI app, `/chat`, `/health`, `/readiness`, `/session` | ✅ `orchestrator/main.py` |
| One shared turn pipeline for HTTP and CLI | ✅ `orchestrator/turn.py` |
| Multi-turn session state | ✅ `orchestrator/session.py`, 90-min TTL |
| Keyword tier below Ollama (works with zero API keys) | ✅ `agents/keyword_intent.py` |
| Synthesis for all four query types | ✅ only `safety_assess` carries a verdict |
| Frontend (React 18 + Vite + TS + MapLibre) | ✅ `frontend/` |
| Generated TS types from Pydantic | ✅ `scripts/gen_types.py`, staleness pinned by test |
| Explainability drawer (drivers, claims, hypotheses, tool calls) | ✅ |
| Cyclone replay | ✅ Fengal / Mandous / Gaja through the production tools |
| Alerts worker (scheduled refresh) | ⬜ `scripts/refresh_cache.py --alerts` is manual |

---

## Verified external facts

Full detail with reproduction steps in `docs/verified_sources.md`.

| Fact | Status |
|---|---|
| Open-Meteo Marine — 11 variables, keyless | ✅ verified live |
| `sea_level_height_msl` exists | ✅ verified (a secondary source said otherwise) |
| Marine grid is **1/12° (~9.3 km)**, not 0.05° | ✅ **measured** |
| Ocean currents in km/h; visibility in metres | ✅ verified unit traps |
| Gaja archive — peak gust 68.6 kn, 2018-11-16 | ✅ verified live |
| INCOIS High Wave Alert bands 3.0 / 3.5 m | ✅ verified |
| INCOIS SVAS scope — 7 m beam width | ✅ verified |
| India–Sri Lanka IMBL — 27 treaty positions | ✅ transcribed from treaty text |
| INCOIS Boat Safety Index formula | ❌ **paywalled — we do not implement or claim it** |
| INCOIS MWW3 at 0.05° | ❌ unverified, from the brief only |

---

## LLM narration — how it is kept safe

The narrator (`agents/narrate.py`) is the one place an LLM touches the **prose**.
It was once the only place an LLM touched a turn at all; there are now three
(planner, domain deliberation, narrator) and the guards below are why the last
of them is safe.

- It sees the **deterministic rendering** of an already-verified object, never
  raw data. It rewrites; it does not reason from evidence.
- **Number guard:** every numeric token in its prose must appear verbatim in
  the rendering it was given. Round 2.44 to 2.4 and the prose is discarded.
- **Verdict guard:** the first sentence must carry the verdict — and, since
  2026-09-06, must not invent one. Only a safety answer has a verdict; a
  fishing-zone answer that opened "do not go out" was shown to a user before
  the prompt distinguished the four answer types.
- **Fallback:** on any failure — guard, refusal, timeout, no key, no network —
  the answer is the template rendering, which is always correct. The system
  never loses the ability to answer.
- **Known limitation, pinned by test:** the guard is a bag of tokens. It can't
  catch a number borrowed from one context ("±2 h") and reused in another
  ("about 2 m"). A positional guard is the fix if live runs ever show it.

Prompt: `agents/prompts/synth.md`. Models: `config/models.yaml`.
Live check: `python scripts/try_narration.py` (needs a key; spends money).

**Live path verified 2026-09-04** via Groq, `openai/gpt-oss-120b`. First
real run passed both guards: verdict in sentence one, downgrade reason in
sentence two, every driver paired with its limit, no number altered.

Facts learned on that run:
- The Groq roster **does not include** Llama 3.3 70B or Mistral Large, the
  models CLAUDE.md named for this tier. `gpt-oss-120b` is the strongest
  available. Re-list `/models` before a demo — an unlisted id fails as a
  quiet fall-through, not a crash.
- Cloudflare rejects `urllib`'s default User-Agent (403 / code 1010). Looks
  exactly like a bad key. Isn't.
- Models emit Unicode hyphens (U+2011). The guard is unaffected — it reads
  digit runs — but the Windows console isn't; stdout is now forced to UTF-8.

Follow-up: the narration dropped the "Based on:" sources line. Before
requiring it in the prompt, make `confidence.basis` human-readable — it
currently says things like `bbox.yaml#reference_points`.

## Decisions worth remembering

- **No model is trained.** Deliberate. A fitted safety classifier could not tell
  a judge why 2.5 m. The audit trail replaces it.
- **Verify against the tool log, not the answer's own evidence block.** An agent
  that invents a number could invent its citation too; verifying a document
  against itself proves nothing.
- **A structured failure does not block dependents.** `active_alerts` returning
  `FAILED` still feeds `compute_risk_score`, which downgrades. Skipping instead
  produced *no verdict* — worse than a cautious one.
- **Thresholding uses the dangerous end of the band**, which depends on the
  threshold's direction. Ceilings use the max, floors use the min.
- **Being at a limit scores 0.60, not 1.0.** Advice that cries wolf gets ignored.
- **A single breach forces `no_go`** regardless of the weighted mean.

## Bugs found by building, worth not reintroducing

| Bug | Caught by |
|---|---|
| `evaluate()` thresholded the max for floors too — 1.5 km fog read as safe | writing `compute_risk_score` |
| Executor skipped the risk step on a failed alert check → **no verdict** | running the real plan |
| Fallback plan hardcoded `alerts_checked: True` | same |
| Renderer said "24 km, under the 2 km limit" | reading the output |
| `bbox.yaml` YAML syntax error — no test had ever loaded the file | boundary code |
| Fixture cited `#wave_height.frp_9m`; config key is `significant_wave_height` | citation round-trip test |
| Window filter dropped the current partial hour | weather tests |
| `model_copy` skips validators, so derived fixtures bypassed every rule | writing fixtures |
| Planner refused `pfz_locate` as `out_of_scope` and told the user we don't locate fishing zones — because the unbuilt tool wasn't advertised | **first live planner run** |
| Chlorophyll anomaly compared a 25 km observed mean against a single nearest baseline pixel → **−8.15 σ** at Point Calimere | reading the output |
| `compose_plan` emitted steps depending on `s1` when no place was given and geospatial contributed nothing | writing the agent test that asserted it returns None |
| Two tests hardcoded "pfz_candidates is unimplemented" as their example of an unbuilt tool, so finishing it made them assert the opposite of the truth | implementing the ocean tools |
| `refresh_points()` was written and **never called by anything** — a fresh checkout had no way to fill the cache | looking for why every weather tool returned FAILED |
| ERDDAP descending latitude axes return an **empty grid, not an error**, when queried low-to-high | first VIIRS fetch |
| `Turn.inheritable` compared state against `"plan"` (planner vocabulary) while `run_turn` records `"answer"` — **multi-turn context silently never worked** | first live two-question session |
| `apply_inheritance` only filled slots the model left None, so slots the model *copied* from context were never recorded — a follow-up silently described a boat the user had not mentioned | same session |
| `fetch_cyclones(now=<historical>)` wrote to the cache keyed by *today*, so a Gaja replay left the live system holding a 2018 storm | testing the replay path |
| Planner emitted `"$s1.lon - 0.5"` — arithmetic inside a step reference. Caught by the validator; prompt now forbids it | first live `causal_explain` |
| **PFZ candidates were recommended without geofencing the destination** — only the departure point was checked, so ORCA could have sent a boat across the IMBL | writing the ocean agent's `review()` |
| A step's `optional` flag was derived from **who asked** rather than from the tool, so every LLM request for a safety-critical tool was auto-rejected as invalid | a deliberating agent correctly asking for a geofence check |
| The English renderer emitted no hypotheses at all, so a causal answer was a headline and a footer with nothing between — and the narrator, given nothing, drifted into generic safety advice | rendering the first LLM-proposed hypotheses |
| The replay printed the **sustained mean** beside a verdict driven by the **gust**, so a correct answer read as "14 kn is over the 25 kn limit" | reading the first replay table |
| Typing **"hello"** after a fishing-zone question returned a full fishing-zone answer — context inheritance manufactured a request the user never made | a user typing hello into the UI |
| A fishing-zone answer opened **"Do not go out until you check the safety forecast"** — the narrator prompt only described safety answers, so a caveat was promoted to the lead and read as a refusal | the same screenshot |
| **An infinite clarification loop.** Asked which port and which boat, the user clicked MECHANISED TRAWLER and was asked the identical question again, forever. `context_for` refuses clarifications by design — correct for the question, wrong for the *reply* to it, which had nowhere to go | clicking an option button in the UI |
| A concern that was mostly digits stripped to the empty string, `Caveat` requires `min_length=1`, and the exception took the whole recommendation with it — "the answer could not be assembled". Intermittent, since it depended on what the model wrote | the same session, one turn later |
| **The query type was carried between turns.** Asked "why has the catch dropped there?" after a fishing-zone question, three turns running answered the fishing-zone question again. The planner was handed `previous_turn.query_type` as neutral context and did the natural thing | running a six-turn conversation |
| The no-LLM fallback copied the previous intent wholesale, so a rate-limited turn answered the previous question — the failure above, surviving even after the prompt was fixed | the same conversation, rate limited |
| `_degraded_cannot_be_go` blocked on the generic `degraded` flag, so when an agent voluntarily added a boundary check (degraded: IMBL present, MPA absent), **checking more turned a valid `go` into a crash** | the first turn of that conversation |
| Synthesis wrote `abs(anomaly_sigma)` into a claim — a number no tool produced. **The verifier caught it** and refused to show the answer, which is exactly its job, and it does not care that the transformation was ours rather than a model's | the fifth turn |

---

## Every agent reasons — and what keeps it safe

Each domain agent gets one LLM call per turn (`deliberator` role,
`openai/gpt-oss-20b`). It sees its own typed findings plus a compact tool
catalogue and returns three things: an **assessment**, **requests** to other
agents, and **concerns** for the caveat block.

Four guards, none of them optional:

1. **It may never write a number.** `strip_numbers()` removes every digit from
   an assessment or concern before it is shown. A caveat reading "waves may
   reach 3 m" would be a safety figure produced by a language model.
2. **A request is validated like any other step.** Unknown tool, bad argument,
   unimplemented tool — dropped, before `validate_plan()` even sees it.
3. **It cannot mark its own request safety-critical.** Critical requests come
   only from the rule floor.
4. **The rule floor always runs.** The model may add requests, never remove
   one. When a malformed model request sinks the batch, the extension is
   retried with the rules alone rather than losing them.

Deliberation degrades to the rules on no key, a timeout, a rate limit or
unparseable output. Verified live on 2026-09-06: on a `pfz_locate` question the
ocean agent independently raised *"we do not know if the destination zone is
within a permitted fishing zone"* — the same gap the hardcoded rule covers,
reached on its own.

**Cost, stated plainly:** up to 5 LLM calls a turn instead of 2. The free Groq
tier rate-limits under rapid use, which is why the deliberation prompt injects
a compact tool catalogue (4.3 kB, not 8.0 kB) and why idle agents are skipped.

## Collaboration — what the rule floor guarantees

| From | To | When | Why |
|---|---|---|---|
| ocean | geospatial | a PFZ candidate exists | `pfz_candidates` knows fronts, not boundaries. The plan geofences the *port*; this geofences the **destination**. Crossing the IMBL means arrest. |
| ocean | weather | candidate ≥ 30 km out | conditions at the zone are not conditions at the port |
| risk | geospatial | verdict is `no_go` or `marginal` | a fisherman told to stay in should be told where to shelter |

**Every request is conditional on runtime data, never on the query type.** The
same safety question produces 0 requests on a calm day and 1 on a bad one:

```
calm, alerts OK   verdict=go     0 requests   33 numbers verified
alert check down  verdict=no_go  1 request    37 numbers verified
```

Bounds, because an unbounded agent loop is a hang: 2 rounds, 4 added steps,
duplicate `(tool, args)` dropped, and a rejected extension keeps the previous
result. A request is put through the *same* `validate_plan()` as any other
step, so an agent cannot smuggle in a call the validator would refuse.

## Causal hypotheses — the LLM widens, code closes

CLAUDE.md has specified this from the start: *the LLM proposes hypotheses,
deterministic code tests them, and untested hypotheses are DROPPED, not
reported.* Until 2026-09-06 `_build_causal` tested exactly two explanations,
because those are the two that were written down.

Now the model proposes from a catalogue of **tests that exist**, and each
proposal is matched to a real function over real tool output:

| Test | Decided by |
|---|---|
| `low_productivity` | chlorophyll anomaly at or below −2σ |
| `bloom_or_turbidity` | the same anomaly at or above +2σ — a high reading near this coast can mean a river plume, not a productive sea |
| `weak_frontal_structure` | strongest SST gradient below 0.05 °C/km |
| `no_reachable_zone` | count of derived zones within the vessel's range |

Three ways a proposal is **dropped and never shown**: no matching test, the
tool did not run, or the tool ran and produced nothing the test can use. A
chlorophyll reading with no baseline is the common case — nothing can be said
about whether productivity is unusual, so nothing is said.

The model cannot change an outcome. It supplies the question and the wording;
the measurement supplies the answer, and `strip_numbers()` removes any figure
it tries to write. The two original hypotheses remain as the floor for when no
model is reachable.

Live on 2026-09-06 at Cuddalore: the model added `bloom_or_turbidity` and
`no_reachable_zone` to the built-in pair, and code returned 2 of 4 supported.

## Cyclone replay — the demo that shows a `no_go`

CLAUDE.md: *a live demo on a calm day proves nothing. Replaying a real cyclone
and watching the risk score go red proves everything.* Every live answer today
is `go`, so the only way to see a refusal was to break the alert source.

```bash
python scripts/replay.py              # Cyclone Fengal
python scripts/replay.py --event gaja # wind only
python scripts/replay.py --list
```

**Nothing is simulated.** Archived observations are written into the cache
format the live tools already read, and then `wave_forecast`, `wind_forecast`
and `compute_risk_score` run against them unmodified, with an explicit `start`.
Replay data goes to `data/replay/<event>/` and never to the live cache — a 2024
storm in today's cache is the mistake a historical GDACS fetch already made once.

**Cyclone Fengal**, Cuddalore, landfall 2024-11-30:

```
28 Nov 00:00   1.28 m   25.1 kn   0.197   no_go   wind_speed over limit
30 Nov 18:00   1.10 m   41.8 kn   0.326   no_go   wind_speed over limit
01 Dec 00:00   1.08 m   48.6 kn   0.326   no_go   wind_speed over limit
01 Dec 21:00   0.72 m   23.1 kn   0.159   go
```

First breach 72 h before landfall; the verdict recovers once the storm passes.

**Why not Gaja by default.** Open-Meteo's marine archive does not reach 2018 —
measured 2026-09-06, wave data begins between Jan 2021 and Nov 2022, and a Gaja
window returns 96 hours of nulls. Gaja is kept and declared WIND ONLY; it
reaches a **68.6 kn** peak gust, independently matching the figure already
verified in this file.

## Still empty, and why

Twelve files remain 0 bytes. None is an oversight:

| File | Why |
|---|---|
| `agents/prompts/synth.md` sibling `_shared.md` | shared fragment, injected — not empty |
| `alerts/worker.py`, `alerts/simulate_track.py` | alerts are pull-only; `refresh_cache.py --alerts` is manual |
| `core/supabase_client.py` | the cache is JSON on disk and adequate; Supabase is a Phase 2 upgrade |
| `ingest/catalogue.py` | CLAUDE.md: *there is no discovery agent* — dataset choice is deterministic |
| `ingest/sources/imd.py` | IMD's APIs return HTTP 401. This is where a key would go |
| `ingest/sources/incois_erddap.py` | INCOIS ERDDAP evaluated and rejected as a live source — archive only |
| `ingest/sources/mosdac.py` | not evaluated |
| `ingest/static/landing_centres.py` | needs the INCOIS set; the 7-point seed stands in |
| `ingest/transform/{clip,regrid,to_zarr}.py` | no Zarr store; layers are cached as JSON |
| `orchestrator/llm/fallback.py` | the fallback chain lives in `client.py` |

## Next up

1. Tamil / regional languages — the remaining explicit ask in the problem
   statement
3. MPA / EEZ geometry — "clear" is still qualified to the IMBL alone
4. Gaja replay — `fetch_archive()` written and unused
