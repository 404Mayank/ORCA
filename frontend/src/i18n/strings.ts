/**
 * ALL user-facing UI chrome lives here. One file, one export.
 *
 * Phase one is English only (same rule as the backend language adapter,
 * which is a pass-through stub). Tamil later is a second object plus a
 * switch: `export const ta: Strings = {...}; setLocale("ta")`. Nothing
 * outside this file may hard-code a sentence the user sees -- placeholders,
 * tooltips, button labels, empty states, sheet headings, all of it.
 *
 * Two things deliberately do NOT live here:
 * - Answer prose. That comes from the API (narrated, number-guarded).
 * - Numbers attached to claims. Those come from tool calls, never from UI
 *   copy. A "stat" line under a task card is a static descriptor, never a
 *   live figure -- the moment it needs to be live it moves into an answer.
 */

export type Locale = "en";

/** Fill a `{slot}` template. Mirrors the backend's claim rendering. */
export function fill(template: string, slots?: Record<string, unknown> | null): string {
  if (!slots) return template;
  return template.replace(/\{(\w+)\}/g, (whole, key) =>
    slots[key] === undefined || slots[key] === null ? whole : String(slots[key]),
  );
}

const en = {
  meta: {
    appName: "ORCA",
    appSub: "Marine Intel",
    documentTitle: "ORCA — Marine Intelligence",
    sector: "South Coromandel sector",
    datumLine: "Datum WGS-84 · soundings in metres",
    scaleLine: "0 — 25 nm",
    disclaimer: "Advisories are guidance, not clearance. Confirm restricted water with the local marine authorities before sailing.",
  },

  boot: {
    steps: ["linking", "reading cache", "checking alerts", "ready"] as string[],
    sub: "Marine intelligence, South Coromandel",
  },

  workspaceNav: "Workspace",
  tasksNav: "Ready tasks",
  sidePanelNav: "Side panel",

  rail: {
    menuOpen: "Open menu",
    menuClose: "Close menu",
    alertsActive: "{n} advisories in force",
    bridge: { label: "Bridge", tip: "Back to the bridge" },
    newer: { label: "New query", tip: "Start a new conversation" },
    conversations: { label: "Conversations", tip: "This session's questions" },
    saved: { label: "Saved zones", tip: "Answers you kept" },
    alerts: { label: "Alerts", tip: "Advisories in force", caption: "alerts" },
    replay: { label: "Replay", tip: "Replay an archived cyclone" },
    settings: { label: "Settings", tip: "Tier, appearance, language, data" },
  },

  topbar: {
    tierFromEnv: "from .env",
    tierFromApp: "set here",
    tierDefault: "default",
    tierOpensSettings: "Open settings",
    feedLive: "weather cached",
    feedEmpty: "weather missing",
    feedUnchecked: "unchecked",
    themeToggle: "Toggle light / dark",
  },

  hero: {
    eyebrow: ["Bay of Bengal", "South Coromandel sector", "10°46′N 79°51′E"] as string[],
    title: "Going out tomorrow?",
    sub: "Ask in plain words — safety, fishing zones, boundaries, or the catch. Every number in an answer comes from a tool call you can inspect.",
  },

  tasks: [
    {
      id: "pfz",
      tint: "teal",
      kind: "Fisheries",
      title: "Where are the fish today",
      body: "Fishing zones from today's sea temperature fronts and chlorophyll.",
      stat: "SST fronts · chlorophyll",
      prompt:
        "Where is the nearest potential fishing zone from Nagapattinam today for my mechanised trawler?",
    },
    {
      id: "safety",
      tint: "green",
      kind: "Safety",
      title: "Is it safe to go out",
      body: "Wind, waves, and tide windows for your next trip, hour by hour.",
      stat: "36 h horizon",
      prompt:
        "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?",
    },
    {
      id: "geofence",
      tint: "red",
      kind: "Compliance",
      title: "Waters to avoid",
      body: "Boundary-line and protected-area alerts for your track.",
      stat: "IMBL + MPA watch",
      prompt: "Which zones must I avoid near Rameswaram?",
    },
    {
      id: "conditions",
      tint: "blue",
      kind: "Conditions",
      title: "Sea conditions",
      body: "Tide, weather, and alerts at a place. Read-only — no verdict.",
      stat: "Tide · weather · alerts",
      prompt: "What are the tide, weather and sea conditions off Cuddalore?",
    },
  ] as { id: string; tint: string; kind: string; title: string; body: string; stat: string; prompt: string }[],

  composer: {
    placeholder: "Ask about the water — zones, weather windows, routes",
    followupPlaceholder: "Ask a follow-up — narrow the radius, change the vessel, check a window",
    send: "Send query",
    sendFollowup: "Send follow-up",
    hintEnter: "Enter",
    hintToSend: "to send",
    hintShiftEnter: "Shift + Enter",
    hintNewLine: "for a new line",
  },

  chips: [
    {
      id: "sst",
      label: "SST fronts",
      prompt:
        "Which regions off Nagapattinam show high chlorophyll concentration and favourable sea surface temperature?",
    },
    {
      id: "swell",
      label: "Swell & tides",
      prompt:
        "What are the swell and tide conditions off Nagapattinam for the next two days?",
    },
    {
      id: "pfz",
      label: "PFZ today",
      prompt: "Where is the nearest fishing zone from Rameswaram for my trawler?",
    },
    {
      id: "boundary",
      label: "Boundary map",
      prompt: "Show the maritime boundary and protected areas near my track from Rameswaram.",
    },
    {
      id: "catch",
      label: "Catch change",
      prompt: "Why has fish productivity declined off Cuddalore?",
    },
    {
      id: "route",
      label: "Safest route",
      prompt:
        "What is the safest route for my mechanised trawler from Nagapattinam considering weather and sea-state conditions?",
    },
  ] as { id: string; label: string; prompt: string }[],

  thread: {
    back: "Back to bridge",
    save: "Keep this answer",
    unsave: "Saved — tap to remove",
    cited: "Read the {n} layers behind this",
    verified: "✓ {n} numbers verified",
    narratedByLlm: "AI rewrite, checked",
    narratedByTemplate: "plain answer",
    fallbackPlan: "fallback plan",
    needsSlots: "Needs: {list}",
    refusalHeading: "Outside our waters",
    verifyFailed: "✗ verification failed",
    degraded: "degraded",
    agentsReasoned: "Each agent reasoned",
    collaborated: "Agents collaborated ({n} round{s})",
    assumptions: "Carrying from earlier",
    hypothesesNote: "Untestable hypotheses are dropped, never shown.",
    queryTitles: {
      pfz_locate: "Potential fishing zones",
      safety_assess: "Voyage safety",
      geofence_check: "Restricted water on your track",
      causal_explain: "Why the catch changed",
      conditions_report: "Sea conditions",
    } as Record<string, string>,
    verdicts: { go: "GO", marginal: "MARGINAL", no_go: "NO GO" } as Record<string, string>,
    tables: {
      driver: "Driver",
      observed: "Observed",
      limit: "Limit",
      status: "Status",
      withinLimit: "Within limit",
      overLimit: "Over limit",
      zone: "Zone",
      run: "Run",
      bearing: "Bearing",
      driversHeading: "What set the verdict",
      zonesHeading: "Candidate zones",
      hypothesesHeading: "Tested explanations",
      supported: "Supported",
      refuted: "Not supported",
      checkedHeading: "Checked and clear",
      windowHeading: "Working window",
      alternativesHeading: "Alternatives",
      claimsHeading: "What the data says",
      guidanceHeading: "Working guidance",
    },
  },

  side: {
    mapTab: "Sector chart",
    evidenceTab: "Evidence",
    legendOrigin: "Departure point",
    legendZone: "Fishing zone",
    legendBox: "Coromandel box",
    legendBoundary: "Maritime boundary",
    readoutNoPosition: "No position in this answer",
    evidenceIntro:
      "Every figure above traces back to a tool call ORCA made. Freshness is measured against the moment of your query.",
    evidenceFoot:
      "Every figure above was matched to the tool call that produced it. Degraded answers say so on the answer itself.",
    sections: {
      verdict: "Verdict",
      noVerdict: "No verdict — this query type does not adjudicate safety.",
      drivers: "Drivers",
      claims: "Claims",
      hypotheses: "Hypotheses tested",
      checked: "Checked and clear",
      assumptions: "Assumptions",
      caveats: "Caveats",
      confidence: "Confidence",
      toolCalls: "Tool calls ({n})",
      trace: "Reasoning trace",
    },
  },

  sheets: {
    close: "Close panel",
    openAction: "Open",
    reaskAction: "Ask again",
    conversations: {
      title: "Conversations",
      sub: "Questions asked in this session, newest last.",
      empty: "Nothing asked yet. The bridge cards are a good first question.",
      newSession: "New session",
    },
    saved: {
      title: "Saved zones",
      sub: "Answers you kept on this device. Leaving the browser does not lose them.",
      empty: "Nothing kept yet. Open an answer and tap “Keep this answer”.",
      open: "Open",
      remove: "Remove",
    },
    alerts: {
      title: "Alerts",
      inForce: "{n} advisor{s} in force",
      noneChecked: "No advisories in force — checked {at}.",
      unchecked:
        "The alert feed has not been checked in this cache, so safety answers qualify accordingly.",
      askAbout: "Ask about alerts off Nagapattinam",
      askPrompt: "Are there any cyclone or high-wave alerts off Nagapattinam?",
    },
    settings: {
      title: "Settings",
      tierHeading: "Reasoning tier",
      tierSub: "Which models reason over your question. Guards and verification are identical on every tier.",
      tiers: {
        free: { name: "Free", desc: "Free Spark models on Zen, then Groq, then offline fallback. Default." },
        fast: { name: "Fast", desc: "Grok + flash models via the Go gateway. Quickest when quota allows." },
        paid: { name: "Strongest", desc: "kimi-k3 + deepseek pro via the Go gateway. Needs quota." },
      } as Record<string, { name: string; desc: string }>,
      useEnv: "Follow .env",
      useEnvSub: "Return authority to ORCA_TIER",
      resetAll: "Reset tuned knobs",
      resetAllSub: "Back to compiled defaults (reasoning, memory, checks, timeouts). Tier untouched.",
      reasoningHeading: "Agent reasoning",
      reasoningSub:
        "When off, agents skip the extra thinking pass and follow fixed rules only. Faster, but they won't ask for follow-up checks on their own.",
      reasoningOn: "On — agents reason",
      reasoningOff: "Off — rules only",
      memoryHeading: "Memory",
      memorySub:
        "How long your place and boat carry over to the next question. Seas change; longer remembers more, shorter stays fresher.",
      memoryPresets: ["30 min", "1½ h", "3 h"] as string[],
      fallbackHeading: "Answer safety net",
      fallbackSub:
        "On, you always get an answer: if the AI rewrite fails its checks, a plain deterministic one stands in. Off means a failed rewrite returns an error instead.",
      fallbackOn: "On — always answer",
      fallbackOff: "Off — error instead",
      roundsHeading: "Follow-up checks",
      roundsSub:
        "How many times agents may extend the plan after the first run. Fewer is faster; more runs extra checks.",
      roundsNone: "None — single run",
      roundsOne: "1 check",
      roundsMany: "{n} checks",
      appearanceHeading: "Appearance",
      light: "Light — sea-glass chart",
      dark: "Dark — abyss console",
      languageHeading: "Language",
      english: "English",
      tamil: "Tamil — phase two, adapter stub in place",
      dataHeading: "Data status",
      dataSub: "What the cache holds, and how old it is. Filled by scripts/refresh_cache.py, never during a question.",
      cached: "cached",
      missing: "missing",
      inForce: "{n} in force",
      layerNames: {
        weather: "Weather",
        sst: "Sea temperature",
        chlorophyll: "Chlorophyll",
        alerts: "Alerts",
      } as Record<string, string>,
      ageHours: "{n} h old",
      ageDays: "{n} d old",
    },
  },

  pipeline: {
    stages: {
      plan: "Reading the question",
      execute: "Reading the sea",
      deliberate: "Agents thinking",
      collaborate: "Agents comparing",
      synthesise: "Assembling the answer",
      verify: "Verifying every number",
      narrate: "Writing it plainly",
    } as Record<string, string>,
  },

  crash: {
    title: "Something broke here",
    body: "The rest of the app is fine. Close this and keep going — and tell the team what you clicked.",
    close: "Close",
  },

  replay: {
    title: "Replay a cyclone",
    sub: "An archived storm through the live tools. Not live conditions.",
    empty: "No archived storms on disk. Fetch one first — see the data status section in Settings.",
    loadError: "Could not load the archive list.",
    run: "Run",
    running: "Running the storm…",
    close: "Close replay",
    tableLabel: "Replay trajectory table",
    noGoAgo: "no_go {h} h before landfall",
    neverRed: "The verdict never reached no_go in this window.",
    firstBreach: "First breach: {t}",
    provenance: "Archived observations, {authority}. Computed live, not recorded.",
    cols: {
      time: "Time",
      wave: "Wave m",
      gust: "Gust kn",
      vis: "Vis km",
      score: "Score",
      verdict: "Verdict",
      why: "Why",
    },
  },

  misc: {
    apiUnreachable: "Could not reach the ORCA API. Is uvicorn running?",
    thinking: "Planning, retrieving, verifying…",
    emptyThread: "Ask about sea safety, fishing zones, maritime boundaries, or why the catch has changed. Every number in an answer comes from a tool call you can inspect.",
    istSuffix: "IST",
  },
};

export type Strings = typeof en;

/** Active locale. Tamil later: add `ta: Strings` and switch this. */
export const str: Strings = en;
