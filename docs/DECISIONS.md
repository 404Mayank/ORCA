# Decisions: why ORCA is the way it is

One line each: decision, date, rationale. New entries go on top with a date.
Reversing one requires updating the code it cites, not just this file.

- **2026-09-08 — Default tier is paid (strongest).** Restarts kept
  landing on the quota-thin free chain; the compiled fallback is now
  paid, and this machine pins `ORCA_TIER=paid`. Fresh clones with no
  keys behave identically (all providers fast-fail to template).
  (`orchestrator/llm/client.py`)

- **2026-09-08 — Shelter-only routing; zero-length legs fail.** Shelter
  resolves to the origin gazetteer point for shore queries, so a same-cell
  leg is FAILED, not a single-point line. Lines on the map need
  origin→zone scope (deferred, owner call). (`route_optimise.py`)
- **2026-09-08 — Driver display names the gust.** The verdict thresholds
  the peak but the table showed the sustained band; now peak-inclusive
  wording generic across drivers. Relational prose still isn't
  verifier-checked — tracked gap. (`templates/en`, `AnswerView`)
- **2026-09-08 — Suggestions generated post-answer, labeled
  model/rules/static/mixed.** Planner-time guesses restarted the
  conversation; the suggest step reads the verified object under a 6 s
  abandon budget. New `suggest` LLM role, 200 tokens. (`agents/suggest.py`)
- **2026-09-08 — Tamil per-language templates, ASCII digits.** Translating
  finished sentences would corrupt numbers; `ta/` templates are authored
  with identical slot keys, parity-tested. Tamil number-words evade the
  digit guard — closed by a dropped-numbers check. (`language/`)
- **2026-09-08 — Readiness enforces staleness.** 12 h weather / 7 d
  satellite gates from `risk_thresholds.yaml`; corrupt cache fails soft;
  alerts reports operator-table age beside the cyclone check.
  (`orchestrator/routes/health.py`)
- **2026-09-08 — Per-turn planner logging.** Fallback rate was
  unmeasurable from logs; one line per turn now. Deterministic
  clarification answers no longer wear the failure badge. (`turn.py`)

- **2026-09-07 — `ORCA_TIER` free/fast/paid.** Free-pool queues dominated turn
  latency (108 s measured); paid Go serves the same turn in 12 s. Tier swaps
  order+chains only; roles, guards, fallbacks untouched. (`client.py`)
- **2026-09-07 — Skip deliberation on read-only turns.** `conditions_report`
  carries no verdict, so deliberation adds no safety-critical request, only
  queued calls. Rule floor still runs. (~36 s saved/turn.)
- **2026-09-07 — Parallel deliberation.** Independent calls ran sequentially
  (~6 s/agent). Thread pool, order-preserving merge. (`collaborate.py`)
- **2026-09-07 — Model chains with per-id transports.** Gateway rosters move;
  a chain that walks dead ids beats a single pinned model. 401 on the first
  id = bad key (terminal); later 401s = model access (walk on). Verified live
  before configuring: roster presence ≠ servable.
- **2026-09-07 — FTS-first RAG, explainer wording only.** Zero new deps
  (httpx + PostgREST); vector column reserved, no query-time embeddings
  (would violate cache-first). `strip_numbers` in code; TESTS gate unchanged.
- **2026-09-07 — No MPA polygon committed.** WDPA mirror queried directly: no
  GoM Marine National Park polygon in the snapshot. A hand-drawn boundary
  that arrests fishermen is worse than an honest gap. Re-check script kept.
- **2026-09-07 — Gazetteer 7→20 with per-point provenance.** Each new point
  OSM-verified independently; confidence 0.85/0.9, never 1.0 without INCOIS.
- **2026-09-07 — `conditions_report` + coded fence.** Homeless queries forced
  through safety gates; new read-only type fenced in code on both tiers
  (safety-decisive = one weight≥2 term), validator bans `compute_risk_score`.
- **2026-09-07 — Hermetic suite.** `.env` auto-load made tests deliberate
  live. `conftest` scrubs credentials; slowness now means a network leak.
- **2026-09-06 — No discovery agent, ever.** Dataset choice is a deterministic
  registry lookup; "autonomous discovery" is a narrative problem, solved by
  framing ("curated registry, agent-selected tools"), not engineering.
- **2026-09-06 — Cache-first, no fetch during queries.** Venue wifi dies;
  nightly ingest + honest staleness (`data_age_days`) instead.
- **2026-09-06 — Derive PFZ, don't fetch it.** Official INCOIS PFZ is
  text/maps; SST fronts ∩ chlorophyll is testable and explainable.
- **2026-09-06 — IMBL digitised from treaty text.** Better than a shapefile
  and a talking point; advisory-only, stated as approximation.
- **2026-09-06 — Operator alert table.** INCOIS high-wave has no machine feed;
  a human-edited table with 24 h expiry beats a permanently-failing check
  (which pinned every verdict to `no_go`).
- **2026-09-06 — Copernicus replaced by CoastWatch.** Keyless L4 SST + DINEOF
  chlorophyll; account dependency removed with no data loss.
- **2026-09-04 — Verifier walks the tool log, not the evidence block.**
  Checking a document against itself proves nothing; caught a real
  `abs(anomaly_sigma)` bug. Hallucination-rejection test pinned.
- **2026-09-04 — No trained model.** A fitted classifier can't answer "why
  2.5 m"; cited thresholds + audit trail can.
