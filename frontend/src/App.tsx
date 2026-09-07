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
import { fill, str } from "./i18n/strings";
import {
  applyTheme,
  loadRecents,
  loadSaved,
  loadTheme,
  pushRecent,
  removeSaved,
  saveAnswer,
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

export default function App() {
  const [theme, setTheme] = useState<Theme>(() => loadTheme());
  const [view, setView] = useState<"bridge" | "thread">("bridge");
  const [railOpen, setRailOpen] = useState(false);
  const [sheet, setSheet] = useState<SheetKey | null>(null);
  const [sheetReturnFocus, setSheetReturnFocus] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Readiness | null>(null);
  const [settings, setSettings] = useState<TierSettings | null>(null);
  const [imbl, setImbl] = useState<Array<[number, number]> | null>(null);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [panel, setPanel] = useState<"map" | "evidence" | null>(null);
  const [trace, setTrace] = useState<StreamEvent[]>([]);
  const [drawer, setDrawer] = useState<Recommendation | null>(null);
  const [mapFor, setMapFor] = useState<Recommendation | null>(null);
  const [turns, setTurns] = useState<SessionTurn[]>([]);
  const [recents, setRecents] = useState<RecentQuery[]>(() => loadRecents());
  const [saved, setSaved] = useState<SavedAnswer[]>(() => loadSaved());

  const sessionId = useRef(newSessionId());
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
    if (!text || busy) return;
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
    setBusy(true);
    const mine = ++reqId.current;
    setTrace([]);
    // A superseded stream keeps running server-side but must not paint
    // into the next question's trace. Gate on the live request id.
    const onEvent = (event: StreamEvent) => {
      if (reqId.current !== mine) return;
      setTrace((t) => [...t, event]);
    };
    const payload = {
      query: text,
      session_id: sessionId.current,
      include_recommendation: true,
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
      if (rec && validOrigin(origin)) {
        setMapFor(rec);
        setPanel("map");
      } else {
        // A follow-up with no position (clarification, chat, refusal) or
        // an answer without one must not leave the previous answer's
        // map/evidence standing beside it.
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
      if (reqId.current === mine) setBusy(false);
    }
  }

  function newSession() {
    // Invalidate any in-flight answer and drop the busy flag with it; the
    // superseded send() above will no-op on resolve instead of appending
    // to the fresh transcript.
    reqId.current++;
    setBusy(false);
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
            busy={busy}
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
                    {trace.length > 0 ? (
                      <PipelineTrace events={trace} />
                    ) : (
                      <div className="answer">
                        <p>{str.misc.thinking}</p>
                      </div>
                    )}
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
