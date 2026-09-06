# Ideal answer — `safety_assess`

**Status: design target, not generated output. Written by hand before
`core/schemas/` was frozen. The schema exists to serialize this.**

This is Open Item #1. Everything in `core/schemas/recommendation.py` must be
able to represent the JSON at the bottom of this file, and the narration layer
must be able to reconstruct the English at the top of this file *from that JSON
alone* — no extra prose stored anywhere.

If the schema cannot express something here, the schema is wrong.
If something here cannot be traced to a `tool_call_id`, the answer is wrong.

---

## The scenario

A fisherman in Nagapattinam (10.77 N, 79.84 E) asks at 18:00 on 14 Nov, in the
northeast monsoon:

> "Is it safe to go out tomorrow morning?"

He has already told us in an earlier turn that he runs a 9 m FRP boat with an
outboard — the modal South Coromandel vessel. Day fishing, VHF but no radar,
returns to the same landing centre.

This scenario is chosen deliberately because **the honest answer is neither yes
nor no.** A system that can only say "safe" or "unsafe" is not useful here, and
a demo built on a flat calm day proves nothing. The interesting answer is
conditional, time-bounded, and offers a fallback.

---

## The English a fisherman should get

> **Marginal — you can go, but be back by 13:00.**
>
> Tomorrow morning is workable. Waves off Nagapattinam are 1.8–2.2 m from the
> northeast at 06:00, under the 2.5 m limit for a 9 m FRP boat. Wind is 15–20
> knots gusting to 25, also northeast.
>
> It does not stay that way. By 14:00 waves build to 2.6–3.1 m, over the limit,
> and the wind gusts to 30 knots. The change is quick — most of the build
> happens between 11:00 and 14:00.
>
> **Your window is 04:00 to 11:00. Start back by 11:00 to be ashore by 13:00.**
>
> There is no cyclone in the Bay of Bengal. There is no INCOIS high-wave
> warning in force for Nagapattinam district. Tide is not a constraint —
> high water is at 05:40, so you have water over the bar for a dawn departure.
>
> **If you want a full day instead:** the Palk Bay side, north of Point
> Calimere, is sheltered from a northeast swell. Waves there stay 0.8–1.2 m all
> day. It is about 55 km further to steam, so it is only worth it if you were
> going to fish a full day anyway.
>
> *Based on Open-Meteo marine forecast issued 14 Nov 12:00 UTC, wave data at
> 0.05°. Confidence is high for the morning and moderate for the afternoon
> build — the timing of the build could move by two hours either way.*

Roughly 200 words. Verdict in the first line. Numbers always paired with the
threshold they are being judged against. The bad news is specific about *when*,
not just *that*. It ends with an option, not a prohibition.

### What is deliberately NOT in the English

- No risk score. "0.4875" means nothing to a fisherman. The score drives the
  verdict internally and appears in the evidence drawer, not the sentence.
- No hedging about model skill in the main body — that goes in the footer.
- No advice we cannot ground. We do not say what to catch or where the fish are;
  that is `pfz_locate`, a different query.

---

## What this forces the schema to support

Each of these is a requirement discovered by writing the English first. This is
the whole reason for doing it in this order.

1. **`verdict` is not a boolean.** It needs at least `go` / `marginal` /
   `no_go`, and `marginal` must be the well-supported case, not an afterthought.

2. **`window` carries three distinct times**, not two: window opens, window
   closes (turn back), and hard ashore-by. "Be back by 13:00" and "start back by
   11:00" are different instructions and both matter.

3. **Every driver needs `observed` AND `threshold` side by side**, plus the
   comparison direction. "1.8–2.2 m against a 2.5 m limit" is the sentence
   pattern, and it must survive translation to Tamil with the numbers intact.
   This is why claims are slot templates.

4. **A driver can be non-breaching now and breaching later.** Wave height is
   *the* driver here and it is under the limit at 06:00 and over it at 14:00.
   The driver needs a time dimension, or the answer collapses into either a
   false "safe" or a false "unsafe".

5. **`negative_findings[]` must be first-class.** "No cyclone active" and "no
   INCOIS warning in force" are the two most reassuring sentences in the whole
   answer. If absence of an alert is modelled as a missing field, they vanish
   and the answer gets worse. A judge will ask "how do you know there is no
   cyclone" and the answer must point at a tool call that checked.

6. **`alternatives[]` needs a cost, not just an option.** "Palk Bay is calmer"
   is close to useless. "Palk Bay is calmer but 55 km further" is actionable.

7. **`confidence` must be per-claim, not just overall.** High for the morning,
   moderate for the afternoon timing — one overall number would be a lie in
   both directions. And `basis` is required text: "forecast issued 6 h ago,
   0.05° wave grid" is what makes the number defensible.

8. **`kind` is about provenance, not epistemics.** A forecast wave height is
   `observed` — meaning it was read verbatim out of a tool's output and the
   verifier can find it in the log. It does not mean the future is known. This
   distinction has to be written down or the whole team will label things
   inconsistently within a day.

9. **Tide is a negative finding here, not a driver.** The schema needs somewhere
   to put "we checked this and it is not the constraint" that is neither a
   driver nor a caveat.

---

## The target JSON

Abbreviated where a block repeats, but every block that the answer needs is
present. `tool_call_id`s are the link the verifier walks.

```jsonc
{
  "schema_version": "1.0.0",
  "query_type": "safety_assess",
  "turn_id": "t_002",

  "verdict": {
    "value": "marginal",              // go | marginal | no_go
    "score": 0.4875,                  // from compute_risk_score(), never the LLM
    "band": "conditional",
    "computed_by": "tc_007"           // the risk function's tool_call_id
  },

  "headline": {
    "template": "Marginal — you can go, but be back by {ashore_by}.",
    "slots": { "ashore_by": "13:00" }
  },

  "window": {
    "opens":      "2025-11-15T04:00:00+05:30",
    "turn_back":  "2025-11-15T11:00:00+05:30",
    "ashore_by":  "2025-11-15T13:00:00+05:30",
    "timezone": "Asia/Kolkata",
    "basis": "tc_003",                // the wave timeseries this was cut from
    "limiting_driver": "wave_height"
  },

  "drivers": [
    {
      "id": "wave_height",
      "label_template": "Waves {min}–{max} {unit} from the {dir}",
      "observed": { "min": 1.8, "max": 2.2, "unit": "m", "qualifier": null },
      "threshold": { "value": 2.5, "unit": "m", "comparison": "lte",
                     "source": "risk_thresholds.yaml#significant_wave_height.frp_9m" },
      "breaching": false,
      "at": "2025-11-15T06:00:00+05:30",
      "trajectory": {                 // requirement 4 — the driver moves
        "direction": "worsening",
        "breaches_at": "2025-11-15T13:00:00+05:30",
        "later": { "min": 2.6, "max": 3.1, "unit": "m",
                   "at": "2025-11-15T14:00:00+05:30" }
      },
      "evidence": ["tc_003"]
    },
    {
      "id": "wind_speed",
      "label_template": "Wind {min}–{max} {unit} gusting {gust}, {dir}",
      "observed": { "min": 15, "max": 20, "unit": "kn", "qualifier": "gusting 25" },
      "threshold": { "value": 25, "unit": "kn", "comparison": "lte",
                     "source": "risk_thresholds.yaml#wind_speed.frp_9m" },
      "breaching": false,
      "at": "2025-11-15T06:00:00+05:30",
      "trajectory": { "direction": "worsening",
                      "breaches_at": "2025-11-15T14:00:00+05:30",
                      "later": { "min": 22, "max": 30, "unit": "kn" } },
      "evidence": ["tc_004"]
    }
  ],

  "negative_findings": [              // requirement 5 — absence is a result
    { "id": "no_cyclone",
      "template": "No cyclone or depression active in the Bay of Bengal.",
      "checked_by": "tc_005", "authority": "IMD",
      "valid_until": "2025-11-15T18:00:00+05:30" },
    { "id": "no_high_wave_warning",
      "template": "No INCOIS high-wave warning in force for {district}.",
      "slots": { "district": "Nagapattinam" },
      "checked_by": "tc_005", "authority": "INCOIS" },
    { "id": "tide_not_limiting",     // requirement 9
      "template": "Tide is not a constraint; high water {hw_time}.",
      "slots": { "hw_time": "05:40" },
      "checked_by": "tc_006" }
  ],

  "claims": [
    { "id": "c1", "kind": "observed",
      "template": "Waves {min}–{max} {unit} from the {dir} at {at}.",
      "slots": { "min": 1.8, "max": 2.2, "unit": "m", "dir": "northeast",
                 "at": "06:00" },
      "evidence": ["tc_003"] },
    { "id": "c2", "kind": "derived",
      "template": "Sea state is moderate and within limits for a {vessel}.",
      "slots": { "vessel": "9 m FRP boat" },
      "derived_by": "rule:sea_state_band", "evidence": ["tc_003", "tc_007"] },
    { "id": "c3", "kind": "derived",
      "template": "Most of the build happens between {from} and {to}.",
      "slots": { "from": "11:00", "to": "14:00" },
      "derived_by": "rule:gradient_window", "evidence": ["tc_003"] },
    { "id": "c4", "kind": "inferred",
      "template": "The afternoon build is the usual northeast monsoon surge.",
      "slots": {},
      "evidence": ["tc_003"] }
    // c4 is inferred: no number attached, so the verifier is lenient.
    // If it carried a number it would have to be traceable or be dropped.
  ],

  "operational_guidance": [
    { "priority": 1,
      "template": "Start back by {turn_back} to be ashore by {ashore_by}.",
      "slots": { "turn_back": "11:00", "ashore_by": "13:00" } },
    { "priority": 2,
      "template": "Keep VHF on channel {ch}; conditions change fast after noon.",
      "slots": { "ch": 16 } }
  ],

  "alternatives": [                   // requirement 6 — option plus its cost
    { "id": "palk_bay",
      "template": "Palk Bay north of Point Calimere stays {min}–{max} {unit} all day.",
      "slots": { "min": 0.8, "max": 1.2, "unit": "m" },
      "cost": { "extra_distance_km": 55, "extra_steam_time_h": 2.4 },
      "verdict": "go",
      "evidence": ["tc_008"] }
  ],

  "spatial_context": {
    "origin": { "name": "Nagapattinam", "lat": 10.77, "lon": 79.84,
                "source": "landing_centres", "resolved_by": "tc_001" },
    "vessel_class": "frp_9m",
    "inherited_from_turn": "t_001",   // schema must survive follow-up turns
    "aoi_bbox": [79.5, 10.3, 80.4, 11.2]
  },

  "assumptions": [
    { "text": "Vessel class carried over from the previous turn.",
      "field": "vessel_class", "value": "frp_9m", "source_turn": "t_001",
      "confirmed_by_user": true }
  ],

  "confidence": {
    "overall": 0.78,
    "by_claim": { "c1": 0.9, "c2": 0.85, "c3": 0.6, "c4": 0.5 },
    "basis": "Open-Meteo marine forecast issued 2025-11-14T12:00Z, 0.05° wave grid, 6 h old. Afternoon build timing uncertain by ±2 h."
  },

  "caveats": [
    { "text": "Timing of the afternoon build may move by two hours either way.",
      "applies_to": ["c3", "window"] }
  ],

  "evidence": [
    { "tool_call_id": "tc_001", "tool": "resolve_place",
      "args": { "name": "Nagapattinam" },
      "output_digest": { "lat": 10.77, "lon": 79.84 },
      "source": "landing_centres", "retrieved_at": "2025-11-14T18:00:12+05:30" },
    { "tool_call_id": "tc_003", "tool": "wave_forecast",
      "args": { "lat": 10.77, "lon": 79.84, "hours": 24 },
      "output_digest": { "hs_min_m": 1.8, "hs_max_m": 3.1, "peak_dir": "NE" },
      "source": "open_meteo_marine", "native_resolution_deg": 0.05,
      "issued_at": "2025-11-14T12:00:00Z", "data_age_days": 0 },
    { "tool_call_id": "tc_007", "tool": "compute_risk_score",
      "args": { "vessel_class": "frp_9m", "wave": "$tc_003", "wind": "$tc_004" },
      "output_digest": { "score": 0.4875, "band": "conditional" },
      "source": "deterministic" }
    // tc_002, tc_004, tc_005, tc_006, tc_008 elided — same shape.
  ],

  "visual_layers": [
    { "type": "point",     "id": "origin",      "ref": "tc_001" },
    { "type": "timeseries","id": "wave_24h",    "ref": "tc_003", "threshold": 2.5 },
    { "type": "polygon",   "id": "palk_bay_alt","ref": "tc_008" }
  ],

  "reasoning_trace": [
    { "step": "s1", "tool": "resolve_place",       "status": "ok", "ms": 12 },
    { "step": "s3", "tool": "wave_forecast",       "status": "ok", "ms": 140 },
    { "step": "s7", "tool": "compute_risk_score",  "status": "ok", "ms": 3 }
  ]
}
```

---

## The three variants the schema must also survive

Written here so they are designed for, not discovered later.

**Degraded — a tool failed.** `wave_forecast` returns, `alerts` times out. The
answer must still render, `negative_findings` loses the cyclone entry, a caveat
appears saying the alert check could not be completed, `confidence.overall`
drops, and the verdict is downgraded — never upgraded — when a safety input is
missing. **A missing alert check can never produce a `go`.**

**Refusal — outside the box.** Asked about Kochi. No verdict, no drivers.
`headline` explains the coverage limit and `spatial_context` records what was
asked for and why it was rejected. This is not an error response; it is a
recommendation object with `verdict: null` and a populated `refusal` reason.

**Follow-up — inherited context.** "What about tomorrow afternoon?" carries
`spatial_context` and `vessel_class` forward from `t_001`, and every inherited
field appears in `assumptions[]` so the user can see what was assumed. Per the
governing rule, a *missing* vessel class is a clarification, never a guess.

---

## Open questions for review

- Is `marginal` the right middle word, or is `caution` clearer once translated
  to Tamil? The word choice is load-bearing and hard to change later.
- Should `window.turn_back` be computed from steam time back to port
  (`distance / vessel_speed`), or a flat safety margin? Computed is more
  defensible to a judge but needs vessel speed in the profile.
- Is a 55 km alternative actually useful, or does it need a distance cap before
  we bother offering it?
