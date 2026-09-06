import { useEffect, useRef, useState } from "react";
import { ask, newSessionId, readiness, type Readiness } from "./api/client";
import type { ChatResponse, Recommendation } from "./types";
import MapView from "./components/MapView";
import EvidenceDrawer from "./components/EvidenceDrawer";

interface Message {
  role: "user" | "bot";
  text: string;
  response?: ChatResponse;
}

const SAMPLES = [
  "Is it safe to take my FRP boat out from Nagapattinam tomorrow morning?",
  "Where is the nearest fishing zone from Rameswaram for my trawler?",
  "Which zones must I avoid near Rameswaram?",
  "Why has my catch declined off Cuddalore?",
];

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<Readiness | null>(null);
  const [drawer, setDrawer] = useState<Recommendation | null>(null);
  const [mapFor, setMapFor] = useState<Recommendation | null>(null);
  // Which side panel is showing, if any. The map used to hold the middle of
  // the screen permanently, which spent the largest area on a basemap that is
  // only meaningful once an answer has a position in it.
  const [panel, setPanel] = useState<"map" | "evidence" | null>(null);
  const sessionId = useRef(newSessionId());
  const feed = useRef<HTMLDivElement>(null);

  // The boot screen is not decoration: it runs the real /readiness check and
  // reports which cache layers answered. An empty cache is the single most
  // likely reason a demo fails, and this is where it becomes visible -- before
  // the first question rather than in its answer.
  const [boot, setBoot] = useState<{ pct: number; label: string } | null>({
    pct: 0,
    label: "linking",
  });

  useEffect(() => {
    const steps = ["linking", "reading cache", "checking alerts", "ready"];
    let i = 0;
    const tick = setInterval(() => {
      i += 1;
      setBoot({ pct: Math.min(100, i * 26), label: steps[Math.min(i, 3)] });
      if (i >= 4) clearInterval(tick);
    }, 260);

    readiness()
      .then(setStatus)
      .catch(() => setStatus(null))
      .finally(() => setTimeout(() => setBoot(null), 1250));
    return () => clearInterval(tick);
  }, []);

  useEffect(() => {
    feed.current?.scrollTo({ top: feed.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function send(query: string) {
    const text = query.trim();
    if (!text || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    try {
      const response = await ask({
        query: text,
        session_id: sessionId.current,
        include_recommendation: true,
      });
      setMessages((m) => [...m, { role: "bot", text: response.answer, response }]);
      const rec = response.recommendation as Recommendation | null;
      if (rec) {
        setMapFor(rec);
        // Open the map only when there is something on it. A refusal or a
        // clarification has no position, and sliding an empty basemap in is
        // motion that tells the reader nothing.
        if (rec.spatial_context?.origin) setPanel("map");
      }
    } catch (error) {
      setMessages((m) => [
        ...m,
        {
          role: "bot",
          // An exception here means the API is unreachable — a different thing
          // from a refusal, which arrives as a normal 200 with a state.
          text: `Could not reach the ORCA API. Is uvicorn running?\n\n${String(error)}`,
          response: undefined,
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {boot && (
        <div className={`boot${boot.pct >= 100 ? " out" : ""}`}>
          <div className="boot-mark">ORCA</div>
          <div className="boot-title">{boot.label}</div>
          <div className="boot-sub">Marine intelligence, South Coromandel</div>
          <div className="boot-bar">
            <i style={{ width: `${boot.pct}%` }} />
          </div>
          <div className="boot-pct">{boot.pct}%</div>
        </div>
      )}

      <div className={`app${panel ? " split" : ""}`}>
      <aside className="sidebar">
        <div className="brand">
          <h1>ORCA</h1>
          <span>Marine Intelligence · SIH 26176</span>
        </div>

        <div className="status">
          {status ? (
            <>
              <span className={`chip ${status.ready ? "ok" : "bad"}`}>
                {status.ready ? "data ready" : "cache empty"}
              </span>
              {Object.entries(status.layers).map(([name, layer]) => (
                <span key={name} className={`chip ${layer.cached ? "" : "bad"}`}>
                  {name}
                  {layer.observation_age_days != null
                    ? ` ${layer.observation_age_days.toFixed(1)}d`
                    : ""}
                </span>
              ))}
              <span className="chip">
                llm:{" "}
                {Object.entries(status.llm_providers)
                  .filter(([, up]) => up)
                  .map(([n]) => n)
                  .join(",") || "none (template)"}
              </span>
            </>
          ) : (
            <span className="chip bad">API unreachable</span>
          )}
        </div>

        <div className="messages" ref={feed}>
          {messages.length === 0 && (
            <div className="msg bot">
              Ask about sea safety, fishing zones, maritime boundaries, or why the
              catch has changed.
              {"\n\n"}Every number in an answer comes from a tool call you can
              inspect.
            </div>
          )}

          {messages.map((message, i) => {
            const r = message.response;
            const rec = r?.recommendation as Recommendation | null | undefined;
            return (
              <div
                key={i}
                className={`msg ${message.role === "user" ? "user" : "bot"}${
                  r?.state === "error" ? " err" : ""
                }`}
              >
                {r?.verdict && (
                  <div className={`verdict ${r.verdict}`}>
                    {r.verdict.replace("_", " ").toUpperCase()}
                  </div>
                )}

                {message.text}

                {r?.options && r.options.length > 0 && (
                  <div className={`options${r.state === "chat" ? " suggest" : ""}`}>
                    {r.options.map((option) => (
                      <button key={option} onClick={() => send(option)}>
                        {option.replace(/_/g, " ")}
                      </button>
                    ))}
                  </div>
                )}

                {r?.agent_reasoning && r.agent_reasoning.length > 0 && (
                  <div className="collab think">
                    <div className="collab-h">Each agent reasoned</div>
                    {r.agent_reasoning.map((line, n) => (
                      <div className="collab-line" key={n}>
                        {line}
                      </div>
                    ))}
                  </div>
                )}

                {r?.collaboration && r.collaboration.length > 0 && (
                  <div className="collab">
                    <div className="collab-h">
                      Agents collaborated ({r.collaboration_rounds} round
                      {r.collaboration_rounds === 1 ? "" : "s"})
                    </div>
                    {r.collaboration.map((line, n) => (
                      <div className="collab-line" key={n}>
                        {line}
                      </div>
                    ))}
                  </div>
                )}

                {r && (
                  <div className="meta">
                    {r.verified === true && <span>✓ {r.numbers_checked} numbers verified</span>}
                    {r.verified === false && <span>✗ verification failed</span>}
                    {r.degraded && <span>degraded</span>}
                    <span>{r.narration_source || r.state}</span>
                    <span>{r.duration_ms} ms</span>
                    {rec && (
                      <>
                        <button
                          onClick={() => {
                            setDrawer(rec);
                            setPanel("evidence");
                          }}
                        >
                          evidence
                        </button>
                        <button
                          onClick={() => {
                            setMapFor(rec);
                            setPanel("map");
                          }}
                        >
                          map
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
          {busy && <div className="msg bot">Planning, retrieving, verifying…</div>}
        </div>

        {messages.length === 0 && (
          <div className="samples">
            {SAMPLES.map((sample) => (
              <button key={sample} onClick={() => send(sample)}>
                {sample}
              </button>
            ))}
          </div>
        )}

        <form
          className="composer"
          onSubmit={(event) => {
            event.preventDefault();
            send(input);
          }}
        >
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Ask about the sea…"
            disabled={busy}
          />
          <button type="submit" disabled={busy || !input.trim()}>
            Ask
          </button>
        </form>
      </aside>

      {panel && (
        <main className="stage">
          <div className="panel-bar">
            <span className="hud">
              {panel === "map"
                ? "South Coromandel · 78.5–82.0 E · 8.0–12.0 N"
                : "Evidence · every number, and the call it came from"}
            </span>
            <div className="panel-tabs">
              <button
                className={panel === "map" ? "on" : ""}
                onClick={() => setPanel("map")}
                disabled={!mapFor}
              >
                map
              </button>
              <button
                className={panel === "evidence" ? "on" : ""}
                onClick={() => setPanel("evidence")}
                disabled={!drawer}
              >
                evidence
              </button>
              <button className="x" onClick={() => setPanel(null)} aria-label="Close panel">
                ×
              </button>
            </div>
          </div>

          {/* The map stays mounted once created: MapLibre re-initialising on
              every tab switch would refetch tiles and lose the camera. */}
          <div className="panel-body" hidden={panel !== "map"}>
            <MapView recommendation={mapFor} />
          </div>
          {panel === "evidence" && drawer && (
            <div className="panel-body scroll">
              <EvidenceDrawer recommendation={drawer} onClose={() => setPanel(null)} />
            </div>
          )}
        </main>
      )}
      </div>
    </>
  );
}
