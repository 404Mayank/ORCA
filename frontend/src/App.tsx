import { useEffect, useRef, useState } from "react";
import {
  ask,
  askStream,
  forgetSession,
  getBoundaries,
  getSettings,
  StreamFailed,
  newSessionId,
  resetKnobs,
  setContextTtl,
  setDeliberating,
  setMaxRounds,
  setTemplateFallback,
  readiness,
  sessionHistory,
  setTier,
  type Readiness,
  type SessionTurn,
  type StreamEvent,
  type TierSettings,
} from "./api/client";
import type { ChatResponse, Recommendation } from "./types";
import AnswerView from "./components/AnswerView";
import Bridge from "./components/Bridge";
import ErrorBoundary from "./components/ErrorBoundary";
import PipelineTrace from "./components/PipelineTrace";
import Composer from "./components/Composer";
import EvidencePane from "./components/EvidencePane";
import Rail, { type RailKey } from "./components/Rail";
import ReplayPanel from "./components/ReplayPanel";
import SectorMap from "./components/SectorMap";
import Sheets, { type SheetKey } from "./components/Sheets";
import type { FeedState } from "./components/Topbar";
import { BackIcon, DocIcon, MapIcon, MenuIcon } from "./components/icons";
import { fill, setActiveLocale, str } from "./i18n/strings";
import {
  applyLocale,
  applyTheme,
  loadLocale,
  loadRecents,
  loadSaved,
  loadTheme,
  pushRecent,
  removeSaved,
  saveAnswer,
  type Locale,
  type RecentQuery,
  type SavedAnswer,
  type Theme,
} from "./storage";

interface Message {
  role: "user" | "bot";
  text: string;
  response?: ChatResponse;
}

function asRecommendation(value: unknown): Recommendation | null {
  // LocalStorage entries survive schema bumps, so a name-only check is not
  // enough: a stale saved object with a string lat would crash SectorMap.
  // Anything failing this check renders as plain prose, never as a chart.
  if (value && typeof value === "object") {
    const v = value as Record<string, unknown>;
    if (typeof v.query_type === "string" && v.query_type.length > 0) {
      return value as Recommendation;
    }
  }
  return null;
}

function validOrigin(origin: { lat: unknown; lon: unknown } | null | undefined): boolean {
  return (
    !!origin &&
    typeof origin.lat === "number" &&
    Number.isFinite(origin.lat) &&
    typeof origin.lon === "number" &&
    Number.isFinite(origin.lon)
  );
}

//: Stable empty trace. A new [] each render would re-fire every effect
//: and memo that depends on it.
const EMPTY_TRACE: StreamEvent[] = [];

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  // Locale mirrors theme: persisted preference, applied instantly. The
  // module binding (`str`) is swapped alongside so every component reads
  // the active locale on its next render; default is English.
  const [locale, setLocaleState] = useState<Locale>(() => loadLocale());
  const [view, setView] = useState<"bridge" | "thread">("bridge");
  const [railOpen, setRailOpen] = useState(false);
  const [sheet, setSheet] = useState<SheetKey | null>(null);
  const [sheetReturnFocus, setSheetReturnFocus] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  // In-flight requests keyed by the session that owns them. A single global
  // `busy` flag caused three separate faults: a second question was refused
  // while any first one flew, switching conversations left the composer dead
  // until the original request landed (forever, if its stream hung), and the
  // bridge painted the thread's progress on the front page. Progress belongs
  // to a conversation, not to the app.
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [status, setStatus] = useState<Readiness | null>(null);
  const [settings, setSettings] = useState<TierSettings | null>(null);
  const [imbl, setImbl] = useState<Array<[number, number]> | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [panel, setPanel] = useState<"map" | "evidence" | null>(null);
  const [traces, setTraces] = useState<Record<string, StreamEvent[]>>({});
  const [drawer, setDrawer] = useState<Recommendation | null>(null);
  const [mapFor, setMapFor] = useState<Recommendation | null>(null);
  const [turns, setTurns] = useState<SessionTurn[]>([]);
  const [recents, setRecents] = useState<RecentQuery[]>(() => loadRecents());
  const [saved, setSaved] = useState<SavedAnswer[]>(() => loadSaved());

  const sessionId = useRef(newSessionId());
  // What the *currently open* conversation is doing. Another conversation
  // still answering in the background is none of this surface's business.
  const busy = Boolean(pending[sessionId.current]);
  const trace = traces[sessionId.current] ?? EMPTY_TRACE;
  // Monotonic send id: a "New query" issued while a question is flying
  // invalidates the in-flight response so it can't append to the fresh
  // transcript or wedge `busy` on.
  const reqId = useRef(0);
  const feed = useRef<HTMLDivElement>(null);
  const [boot, setBoot] = useState<{ pct: number; label: string } | null>({
    pct: 0,
    label: str.boot.steps[0],
  });

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    applyLocale(locale);
    setActiveLocale(locale);
    document.title = str.meta.documentTitle;
  }, [locale]);

  // Boot runs the real checks: cache readiness and tier state. An empty
  // cache is the likeliest demo failure, and this is where it surfaces --
  // before the first question, not inside its answer.
  useEffect(() => {
    // Promise-driven, not timer-driven: the bar may show at most 90% until
    // both checks settle, and dismisses only after they have. A slow
    // /readiness can no longer be papered over by the ceremony.
    const started = Date.now();
    const steps = str.boot.steps;
    // Ticks never show the final "ready" label: only the dismiss path may
    // claim it, and only after both checks actually settled.
    const lastTick = steps.length - 2;
    let i = 0;
    const tick = setInterval(() => {
      i += 1;
      setBoot({ pct: Math.min(90, i * 24), label: steps[Math.min(i, lastTick)] });
    }, 260);
    const timers: number[] = [];
    const dismiss = () => {
      clearInterval(tick);
      const wait = Math.max(0, 900 - (Date.now() - started));
      timers.push(window.setTimeout(() => {
        setBoot({ pct: 100, label: steps[steps.length - 1] });
        timers.push(window.setTimeout(() => setBoot(null), 350));
      }, wait));
    };
    let pending = 2;
    const settle = () => {
      pending -= 1;
      if (pending <= 0) dismiss();
    };
    readiness()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(settle);
    // Static treaty line for the chart. Fails silently: the map stays
    // honest without it (legend names only what is drawn).
    getBoundaries()
      .then((b) => setImbl(b.imbl))
      .catch(() => setImbl(null));
    getSettings()
      .then(setSettings)
      .catch(() => setSettings(null))
      .finally(settle);
    return () => {
      clearInterval(tick);
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, []);

  useEffect(() => {
    feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: "smooth" });
  }, [messages, view]);

  // Bridge entries (cards, chips, bridge composer) always start a NEW
  // conversation: a fresh session with no inherited slots. Continuing the
  // old thread from a surface that reads as "ask something new" is how a
  // fishing-zone question silently inherits yesterday's port. The thread
  // composer and its option buttons continue the session instead.
  async function send(query: string, opts?: { fresh?: boolean }) {
    const text = query.trim();
    // Only a question aimed at a conversation that is *already answering* is
    // refused, and a fresh send is never that: it rotates to a new session
    // below, which cannot be pending. Checking the outgoing session here is
    // what still blocked "ask something new" while the old thread flew.
    // Two conversations may fly at once -- the backend keys its session store
    // the same way, and /chat/stream is covered for concurrent turns by
    // tests/test_stream.py.
    if (!text) return;
    if (!opts?.fresh && pending[sessionId.current]) return;
    if (opts?.fresh) {
      // A fresh question orphans the previous server session: forget it
      // so it can't be inherited from, then rotate the id.
      const old = sessionId.current;
      sessionId.current = newSessionId();
      void forgetSession(old).catch(() => {});
      setMessages([]);
      setPanel(null);
      setDrawer(null);
      setMapFor(null);
      setTurns([]);
    }
    setSheet(null);
    setReplayOpen(false);
    setView("thread");
    setMessages((m) => [...m, { role: "user", text }]);
    // Captured once. `sessionId.current` can rotate under a long flight (the
    // user starts something new), and every write below must land on the
    // conversation that actually asked -- not on whichever one is open when
    // the answer arrives.
    const owner = sessionId.current;
    setPending((p) => ({ ...p, [owner]: true }));
    const mine = ++reqId.current;
    setTraces((t) => ({ ...t, [owner]: [] }));
    // A superseded stream keeps running server-side but must not paint
    // into the next question's trace. Gate on the live request id.
    const onEvent = (event: StreamEvent) => {
      if (reqId.current !== mine) return;
      setTraces((t) => ({ ...t, [owner]: [...(t[owner] ?? []), event] }));
    };
    const payload = {
      query: text,
      session_id: owner,
      include_recommendation: true,
      // The chrome locale IS the answer language. Slice 1 shipped the switch
      // without this line, so a Tamil UI asked for and got English prose.
      // An unrenderable tag falls back server-side with a recorded note.
      language: locale,
    };
    try {
      // Live trace first; any transport trouble falls back to the
      // identical blocking route. A partial answer is never shown.
      let response: ChatResponse;
      try {
        response = await askStream(payload, onEvent);
      } catch (error) {
        if (!(error instanceof StreamFailed)) throw error;
        response = await ask(payload);
      }
      if (reqId.current !== mine) return; // superseded by New query
      setMessages((m) => [...m, { role: "bot", text: response.answer, response }]);
      setRecents(pushRecent({ query: text, verdict: response.verdict ?? null, at: Date.now() }));
      const rec = asRecommendation(response.recommendation);
      const origin = rec?.spatial_context?.origin;
      // Both side-panel tabs bind to THIS answer, together, every turn.
      //
      // `drawer` used to be set only by the "read the layers behind this"
      // button, which had two consequences. The Evidence tab sat disabled
      // after a fresh answer until you clicked that button -- the panel
      // offered a tab it would not open. And once clicked, `drawer` held
      // that recommendation for the rest of the session: the next answer
      // replaced the map but not the evidence, so the pane quietly listed
      // the previous answer's tool calls beside the current one. In a
      // system whose whole claim is that every number traces to a tool
      // call, evidence for a different question is the worst thing this
      // panel can show.
      if (rec) {
        setDrawer(rec);
        if (validOrigin(origin)) {
          setMapFor(rec);
          setPanel("map");
        } else {
          // Answerable but unplaced (causal, a conditions read with no
          // fix). There is no map to show, so the evidence is the panel --
          // and the stale one must go with it.
          setMapFor(null);
          setPanel("evidence");
        }
      } else {
        // A clarification, chat reply or refusal carries no recommendation.
        // Nothing about the previous answer may stand beside it.
        setDrawer(null);
        setMapFor(null);
        setPanel(null);
      }
    } catch (error) {
      if (reqId.current !== mine) return; // superseded by New query
      // Raw error internals stay in the console; the user gets a sentence.
      // eslint-disable-next-line no-console
      console.error("[orca] ask failed:", error);
      setMessages((m) => [
        ...m,
        {
          role: "bot",
          // Unreachable API is infrastructure, not a refusal (refusals are
          // 200s with a state). It reads as a sentence, not an error page.
          text: str.misc.apiUnreachable,
          response: undefined,
        },
      ]);
    } finally {
      // Always clear the owner's flag, superseded or not. Gating this on the
      // live request id is what wedged the composer: a request invalidated
      // by a session rotation left `pending[owner]` set forever, and
      // returning to that conversation found it permanently answering.
      setPending((p) => {
        const next = { ...p };
        delete next[owner];
        return next;
      });
    }
  }

  function newSession() {
    // Invalidate any in-flight answer and drop the busy flag with it; the
    // superseded send() above will no-op on resolve instead of appending
    // to the fresh transcript.
    reqId.current++;
    const old = sessionId.current;
    sessionId.current = newSessionId();
    void forgetSession(old).catch(() => {});
    setMessages([]);
    setPanel(null);
    setDrawer(null);
    setMapFor(null);
    setTurns([]);
    setSheet(null);
    setReplayOpen(false);
    setView("bridge");
  }

  async function openSheet(key: SheetKey) {
    setSheet(key);
    if (key === "conversations") {
      try {
        setTurns(await sessionHistory(sessionId.current));
      } catch {
        setTurns([]);
      }
    }
    if (key === "settings" && !settings) {
      try {
        setSettings(await getSettings());
      } catch {
        /* offline -- the sheet shows tier controls disabled */
      }
    }
  }

  const [replayOpen, setReplayOpen] = useState(false);

  function onRail(key: RailKey) {
    setRailOpen(false);
    setSheetReturnFocus(
      key === "conversations" ? str.rail.conversations.label
      : key === "saved" ? str.rail.saved.label
      : key === "alerts" ? str.rail.alerts.label
      : key === "settings" ? str.rail.settings.label
      : null,
    );
    if (key === "bridge") {
      setSheet(null);
      setReplayOpen(false);
      setView("bridge");
    } else if (key === "new") {
      newSession();
    } else if (key === "replay") {
      setSheet(null);
      setReplayOpen(true);
    } else {
      setReplayOpen(false);
      void openSheet(key);
    }
  }

  async function changeTier(tier: string | null) {
    setSettingsBusy(true);
    try {
      setSettings(await setTier(tier));
    } catch {
      /* unreachable API -- controls stay, tier unchanged */
    } finally {
      setSettingsBusy(false);
    }
  }

  async function changeDeliberating(value: boolean) {
    setSettingsBusy(true);
    try {
      setSettings(await setDeliberating(value));
    } catch {
      /* unreachable API -- controls stay, value unchanged */
    } finally {
      setSettingsBusy(false);
    }
  }

  // One shared runner for the simple knobs: same busy discipline as tier.
  async function changeSetting(work: () => Promise<TierSettings>) {
    setSettingsBusy(true);
    try {
      setSettings(await work());
    } catch {
      /* unreachable API -- controls stay, value unchanged */
    } finally {
      setSettingsBusy(false);
    }
  }

  /** Jump to an answer already in the transcript. No API call. */
  function openTurn(turnId: string) {
    setSheet(null);
    setView("thread");
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document
          .getElementById(`turn-${turnId}`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
  }

  function openSaved(entry: SavedAnswer) {
    const rec = asRecommendation(entry.recommendation);
    const response = {
      turn_id: entry.id,
      state: "answer",
      answer: entry.answer,
      verdict: entry.verdict,
      options: [],
      verified: entry.verified ?? null,
      numbers_checked: entry.numbers_checked ?? 0,
      degraded: entry.degraded ?? false,
      collaboration: [],
      collaboration_rounds: 0,
      agent_reasoning: [],
      narration_source: entry.narration_source ?? "",
      duration_ms: entry.duration_ms ?? 0,
      llm_provider: "none",
      used_fallback_plan: false,
      notes: [],
      recommendation: entry.recommendation,
    } as unknown as ChatResponse;
    setSheet(null);
    setView("thread");
    setMessages([
      { role: "user", text: entry.query },
      { role: "bot", text: entry.answer, response },
    ]);
    if (rec) {
      setMapFor(rec);
      setDrawer(rec);
      setPanel(rec.spatial_context?.origin ? "map" : "evidence");
    } else {
      setPanel(null);
    }
  }

  const alertsLayer = status?.layers?.["alerts"];
  const alertBadge =
    alertsLayer?.cached && (alertsLayer.in_force ?? 0) > 0 ? (alertsLayer.in_force as number) : null;
  const feedState: FeedState = !status ? "unknown" : status.ready ? "live" : "empty";

  const botMessages = messages.filter((m) => m.role === "bot" && m.response);
  const lastBot = botMessages[botMessages.length - 1];
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  // Thread title anchors to the FIRST exchange, not the last: a thread is
  // named for what started it, and the title must not drift every time a
  // later question classifies differently. An opening exchange with no
  // recommendation (clarification, chat, refusal) leaves first-user-text
  // as the permanent title even if a later answer classifies -- stable is
  // correct.
  const firstBot = botMessages[0];
  const firstRec = firstBot ? asRecommendation(firstBot.response!.recommendation) : null;
  const firstUser = messages.find((m) => m.role === "user");
  const threadTitle = firstRec
    ? (str.thread.queryTitles[firstRec.query_type] ?? firstRec.query_type)
    : (firstUser?.text.slice(0, 48) ?? str.sheets.conversations.title);
  const lastSaved = lastBot != null && saved.some((s) => s.id === lastBot.response!.turn_id);

  function toggleSave() {
    if (!lastBot?.response || !lastUser) return;
    const id = lastBot.response.turn_id;
    if (saved.some((s) => s.id === id)) {
      removeSaved(id);
      setSaved(loadSaved());
    } else {
      const entry: SavedAnswer = {
        id,
        query: lastUser.text,
        answer: lastBot.text,
        verdict: lastBot.response.verdict ?? null,
        at: Date.now(),
        recommendation: (lastBot.response.recommendation as Record<string, unknown> | null) ?? null,
        verified: lastBot.response.verified ?? null,
        numbers_checked: lastBot.response.numbers_checked ?? 0,
        narration_source: lastBot.response.narration_source ?? "",
        degraded: lastBot.response.degraded ?? false,
        duration_ms: lastBot.response.duration_ms ?? 0,
      };
      saveAnswer(entry);
      setSaved(loadSaved());
    }
  }

  return (
    <>
      {boot && (
        <div className={`boot${boot.pct >= 100 ? " out" : ""}`}>
          <div className="boot-mark">ORCA</div>
          <div className="boot-title">{boot.label}</div>
          <div className="boot-sub">{str.boot.sub}</div>
          <div className="boot-bar">
            <i style={{ width: `${boot.pct}%` }} />
          </div>
          <div className="boot-pct">{boot.pct}%</div>
        </div>
      )}

      <div className="frame">
        <Rail
          active={replayOpen ? "replay" : (sheet ?? (view === "bridge" ? "bridge" : null))}
          alertBadge={alertBadge}
          alertsCached={alertsLayer ? alertsLayer.cached : false}
          onNav={onRail}
          open={railOpen}
          onClose={() => setRailOpen(false)}
        />

        {view === "bridge" ? (
          <Bridge
            theme={theme}
            settings={settings}
            feed={feedState}
            // Never another conversation's state. Sending from the bridge
            // starts a *fresh* session and switches to the thread in the same
            // synchronous block, so the bridge can never be the surface that
            // owns a flight -- and a thread still answering in the background
            // must not paint its pipeline across the front page.
            busy={false}
            trace={EMPTY_TRACE}
            onSend={(text) => void send(text, { fresh: true })}
            onOpenSettings={() => {
              setSheetReturnFocus(str.rail.settings.label);
              void openSheet("settings");
            }}
            onMenu={() => setRailOpen(true)}
            onToggleTheme={() => setTheme((t) => (t === "light" ? "dark" : "light"))}
          />
        ) : (
          <section className="thread">
            <div className="convo">
              <div className="convo-bar">
                <button
                  className="icon-btn menu-btn"
                  onClick={() => setRailOpen(true)}
                  aria-label={str.rail.menuOpen}
                  title={str.rail.menuOpen}
                >
                  <MenuIcon />
                </button>
                <button
                  className="back"
                  onClick={() => {
                    setSheet(null);
                    setView("bridge");
                  }}
                  aria-label={str.thread.back}
                  title={str.thread.back}
                >
                  <BackIcon />
                </button>
                <div className="convo-id">
                  <b>{threadTitle}</b>
                  <span>
                    {settings?.tier ?? "…"} · {str.meta.sector}
                  </span>
                </div>
                {lastBot && (
                  <button
                    className="save-btn"
                    aria-pressed={lastSaved}
                    onClick={toggleSave}
                    title={lastSaved ? str.thread.unsave : str.thread.save}
                  >
                    {lastSaved ? str.thread.unsave : str.thread.save}
                  </button>
                )}
                <div className="feed" title={str.sheets.settings.dataSub}>
                  <span
                    className={`beacon ${feedState === "live" ? "" : feedState === "empty" ? "bad" : "warn"}`}
                    aria-hidden="true"
                  />
                  <span>
                    {feedState === "live"
                      ? str.topbar.feedLive
                      : feedState === "empty"
                        ? str.topbar.feedEmpty
                        : str.topbar.feedUnchecked}
                  </span>
                </div>
              </div>

              <div className="scroll" ref={feed}>
                {messages.length === 0 && <div className="msg">{str.misc.emptyThread}</div>}
                {messages.map((message, i) =>
                  message.role === "user" ? (
                    <div className="msg user enter" key={i}>
                      <div className="bubble">{message.text}</div>
                    </div>
                  ) : (
                    <div
                      className="msg enter"
                      key={i}
                      id={message.response ? `turn-${message.response.turn_id}` : undefined}
                    >
                      <div className="who">
                        <span className="mark" aria-hidden="true">
                          {str.meta.appName.slice(0, 1)}
                        </span>
                        {str.meta.appName}
                      </div>
                      {message.response ? (
                        <AnswerView
                          response={message.response}
                          onSend={send}
                          busy={busy}
                          onOpenEvidence={() => {
                            const rec = asRecommendation(message.response!.recommendation);
                            if (rec) {
                              setDrawer(rec);
                              setPanel("evidence");
                            }
                          }}
                        />
                      ) : (
                        <div className="answer">
                          <p>{message.text}</p>
                        </div>
                      )}
                    </div>
                  ),
                )}
                {busy && (
                  <div className="msg">
                    <div className="who">
                      <span className="mark" aria-hidden="true">
                        {str.meta.appName.slice(0, 1)}
                      </span>
                      {str.meta.appName}
                    </div>
                    {/* The brewing card covers the zero-event gap itself
                        (roadmap + elapsed clock), so there is no flat
                        "thinking" paragraph any more. */}
                    <PipelineTrace events={trace} />
                  </div>
                )}
              </div>

              <div className="thread-composer">
                <Composer
                  compact
                  busy={busy}
                  placeholder={str.composer.followupPlaceholder}
                  onSend={send}
                />
              </div>
            </div>

            {panel && (
              <ErrorBoundary key={`side-${panel}`} label="side panel" onClose={() => setPanel(null)}>
              <aside className="side">
                <div className="side-head">
                  <div className="seg" role="tablist" aria-label={str.sidePanelNav}>
                    <button
                      role="tab"
                      aria-selected={panel === "map"}
                      onClick={() => setPanel("map")}
                      disabled={!mapFor}
                    >
                      <MapIcon />
                      {str.side.mapTab}
                    </button>
                    <button
                      role="tab"
                      aria-selected={panel === "evidence"}
                      onClick={() => setPanel("evidence")}
                      disabled={!drawer}
                    >
                      <DocIcon />
                      {str.side.evidenceTab}
                    </button>
                  </div>
                  <span className="stamp">
                    {panel === "map"
                      ? str.meta.sector
                      : fill(str.side.sections.toolCalls, { n: drawer?.evidence?.length ?? 0 })}
                  </span>
                </div>

                {/* The map stays mounted behind the evidence tab: MapLibre
                    re-initialising on every switch would refetch tiles and
                    lose the camera. */}
                <div style={{ display: panel === "map" ? "contents" : "none" }}>
                  <SectorMap recommendation={mapFor} imbl={imbl} />
                </div>
                {panel === "evidence" && drawer && <EvidencePane recommendation={drawer} />}
              </aside>
              </ErrorBoundary>
            )}
          </section>
        )}

        {sheet && (
          <ErrorBoundary key={sheet} label={`sheet:${sheet}`} onClose={() => setSheet(null)}>
          <Sheets
            sheet={sheet}
            onClose={() => setSheet(null)}
            turns={turns}
            recents={recents}
            onReask={(q) => send(q, { fresh: true })}
            onNewSession={newSession}
            saved={saved}
            answerTurnIds={botMessages.map((m) => m.response!.turn_id)}
            onOpenTurn={openTurn}
            busy={busy}
            onOpenSaved={openSaved}
            onRemoveSaved={(id) => {
              removeSaved(id);
              setSaved(loadSaved());
            }}
            alerts={
              alertsLayer
                ? {
                    cached: alertsLayer.cached,
                    in_force: alertsLayer.in_force,
                    retrieved_at: alertsLayer.retrieved_at,
                    operator_checked: alertsLayer.operator_checked,
                    operator_age_hours: alertsLayer.operator_age_hours,
                    operator_max_age_hours: alertsLayer.operator_max_age_hours,
                  }
                : null
            }
            onAskAlerts={() => send(str.sheets.alerts.askPrompt, { fresh: true })}
            settings={settings}
            settingsBusy={settingsBusy}
            onSetTier={(t) => void changeTier(t)}
            onSetDeliberating={(v) => void changeDeliberating(v)}
            onSetContextTtl={(m) => void changeSetting(() => setContextTtl(m))}
            onSetTemplateFallback={(v) => void changeSetting(() => setTemplateFallback(v))}
            onSetMaxRounds={(r) => void changeSetting(() => setMaxRounds(r))}
            onResetKnobs={() => void changeSetting(() => resetKnobs())}
            returnFocusLabel={sheetReturnFocus}
            hint={status?.hint ?? null}
            theme={theme}
            onSetTheme={(t) => setTheme(t)}
            locale={locale}
            // Swap the strings binding BEFORE setState re-renders: the
            // effect below also swaps (idempotent), but effects run after
            // the render pass, which would paint one stale-locale frame.
            onSetLocale={(l) => { setActiveLocale(l); setLocaleState(l); }}
            layers={status?.layers ?? {}}
          />
          </ErrorBoundary>
        )}
        {replayOpen && (
          <ErrorBoundary key="replay" label="sheet:replay" onClose={() => setReplayOpen(false)}>
            <ReplayPanel onClose={() => setReplayOpen(false)} />
          </ErrorBoundary>
        )}
      </div>

    </>
  );
}
