import { useEffect, useMemo, useRef, useState } from "react";
import { fill, str } from "../i18n/strings";
import type { StreamEvent } from "../api/client";

/**
 * The wait state: a live view of the agents working.
 *
 * It renders ONLY what the backend's ProgressBus reported. The stage spine
 * mirrors orchestrator/progress.py STAGES (showing the roadmap ahead is
 * honest -- the sequence is fixed and deterministic), and every line in the
 * conversation comes from a `deliberate` or `collaborate` frame. Every
 * model-written string in those frames is number-stripped at its source in
 * agents/deliberate.py, which is why it is safe to show verbatim.
 *
 * No percentages, no ETA, no invented activity. The only live number is the
 * client-side elapsed clock, which measures the wait rather than the system.
 *
 * Why a conversation rather than a checklist: the headline claim of this
 * project is that several agents reason and ask each other for things. A
 * seven-row checklist of grey labels proves none of that -- it is a spinner
 * with extra steps, and it looked identical whether one agent ran or four.
 */

const ROADMAP = [
  "plan",
  "execute",
  "deliberate",
  "collaborate",
  "synthesise",
  "verify",
  "narrate",
] as const;

type Line =
  | { kind: "say"; id: string; agent: string; text: string; usedLlm: boolean }
  | {
      kind: "ask";
      id: string;
      from: string;
      to: string;
      tool: string;
      reason: string;
      rule: boolean;
      /** The collaborate round accepted it and ran it, not merely proposed. */
      accepted: boolean;
    }
  | { kind: "flag"; id: string; agent: string; text: string };

/** from|to|tool identifies one request across the two frames that mention it. */
const askKey = (from: string, to: string, tool: string) => `${from}|${to}|${tool}`;

interface Props {
  events: StreamEvent[];
  /** Bridge variant: tighter chrome under the composer. */
  compact?: boolean;
}

/** "RiskAgent" -> "Risk". A fixed suffix strip; nothing is invented. */
const short = (agent: string) => agent.replace(/Agent$/, "");

/**
 * Reveal `text` a character at a time.
 *
 * Only ever applied to the newest line: re-typing settled lines on every
 * render would make the panel jitter, and a line that has already been read
 * should not move. Honours prefers-reduced-motion by returning the whole
 * string immediately -- the animation is decoration, the text is the content.
 */
function useTyped(text: string, active: boolean): string {
  const [shown, setShown] = useState(active ? "" : text);
  const reduced = useRef(false);
  useEffect(() => {
    reduced.current =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  }, []);
  useEffect(() => {
    if (!active || reduced.current) {
      setShown(text);
      return;
    }
    setShown("");
    let i = 0;
    const step = window.setInterval(() => {
      i += 2;
      setShown(text.slice(0, i));
      if (i >= text.length) window.clearInterval(step);
    }, 16);
    return () => window.clearInterval(step);
  }, [text, active]);
  return shown;
}

function TypedText({ text, active }: { text: string; active: boolean }) {
  const shown = useTyped(text, active);
  return (
    <>
      {shown}
      {active && shown.length < text.length && <span className="brew-caret" aria-hidden="true" />}
    </>
  );
}

export default function PipelineTrace({ events, compact }: Props) {
  // Local wall clock, ticked client-side. It measures the wait, never the
  // system: no percentage, no ETA -- those are unknowable by design.
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  const last = events.length > 0 ? events[events.length - 1] : null;
  // "done" means the turn finished; treat every stage as completed. With
  // ZERO events (planner still queued) the honest state is every stage
  // pending: nothing has happened yet.
  const lastStage = last ? (last.stage === "done" ? "narrate" : last.stage) : null;
  const lastIdx = lastStage ? ROADMAP.indexOf(lastStage as (typeof ROADMAP)[number]) : -1;

  const { lines, agents } = useMemo(() => {
    const out: Line[] = [];
    const seen: string[] = [];
    // A request is announced twice -- once by the agent that proposed it in
    // its `deliberate` frame, once by the `collaborate` frame that accepted
    // and ran it. Rendered as two lines they read as the system repeating
    // itself. The second mention upgrades the first instead.
    const askAt = new Map<string, number>();
    events.forEach((e, i) => {
      if (e.stage === "deliberate") {
        const agent = typeof e.agent === "string" ? e.agent : "";
        if (!agent) return;
        if (!seen.includes(agent)) seen.push(agent);
        if (typeof e.assessment === "string" && e.assessment.trim()) {
          out.push({
            kind: "say",
            id: `${i}-say`,
            agent,
            text: e.assessment.trim(),
            usedLlm: e.used_llm === true,
          });
        }
        const asks = Array.isArray(e.asks) ? e.asks : [];
        asks.forEach((raw, j) => {
          const ask = raw as Record<string, unknown>;
          if (typeof ask.to_agent !== "string" || typeof ask.tool !== "string") return;
          askAt.set(askKey(agent, ask.to_agent, ask.tool), out.length);
          out.push({
            kind: "ask",
            id: `${i}-ask-${j}`,
            from: agent,
            to: ask.to_agent,
            tool: ask.tool,
            reason: typeof ask.reason === "string" ? ask.reason : "",
            rule: false,
            accepted: false,
          });
        });
        const concerns = Array.isArray(e.concerns) ? e.concerns : [];
        concerns.forEach((c, j) => {
          if (typeof c !== "string" || !c.trim()) return;
          out.push({ kind: "flag", id: `${i}-flag-${j}`, agent, text: c.trim() });
        });
      }
      if (e.stage === "collaborate") {
        const requests = Array.isArray(e.requests) ? e.requests : [];
        requests.forEach((raw, j) => {
          const req = raw as Record<string, unknown>;
          if (typeof req.from_agent !== "string" || typeof req.to_agent !== "string") return;
          if (typeof req.tool !== "string") return;
          const key = askKey(req.from_agent, req.to_agent, req.tool);
          const at = askAt.get(key);
          if (at !== undefined) {
            const existing = out[at];
            if (existing.kind === "ask") {
              // Same request, now confirmed. Upgrade in place.
              out[at] = { ...existing, accepted: true, rule: existing.rule || req.critical === true };
            }
            return;
          }
          askAt.set(key, out.length);
          out.push({
            kind: "ask",
            id: `${i}-req-${j}`,
            from: req.from_agent,
            to: req.to_agent,
            tool: req.tool,
            reason: typeof req.reason === "string" ? req.reason : "",
            // The rule floor produced this, not a model. The UI is entitled
            // to say which is which -- they carry different weight.
            rule: req.critical === true,
            accepted: true,
          });
        });
      }
    });
    return { lines: out, agents: seen };
  }, [events]);

  // Keep the newest line in view without yanking the page around it.
  const tail = useRef<HTMLDivElement>(null);
  useEffect(() => {
    tail.current?.scrollTo({ top: tail.current.scrollHeight, behavior: "smooth" });
  }, [lines.length]);

  const activeAgent =
    last?.stage === "deliberate" && typeof last.agent === "string" ? last.agent : null;

  return (
    <div className={`brew${compact ? " compact" : ""}`} role="status" aria-live="polite">
      <div className="brew-head">
        <span className="beacon" aria-hidden="true" />
        <span className="brew-title">{str.pipeline.brewing}</span>
        {agents.length > 0 && (
          <span className="brew-agents">
            {agents.map((a) => (
              <span key={a} className={`brew-agent${a === activeAgent ? " active" : ""}`}>
                {short(a)}
              </span>
            ))}
          </span>
        )}
        <span className="brew-elapsed">{fill(str.pipeline.elapsed, { n: String(elapsed) })}</span>
      </div>

      {/* The roadmap, as a spine rather than a checklist: one lit label and
          a segmented track, so seven grey rows stop competing with the
          conversation for attention. */}
      <div className="brew-spine">
        <ol className="brew-track">
          {ROADMAP.map((stage, i) => (
            <li
              key={stage}
              className={`brew-seg${i < lastIdx ? " done" : i === lastIdx ? " active" : " todo"}`}
              title={str.pipeline.stages[stage] ?? stage}
            />
          ))}
        </ol>
        <span className="brew-stage-now">
          {lastIdx >= 0
            ? (str.pipeline.stages[ROADMAP[lastIdx]] ?? ROADMAP[lastIdx])
            : str.pipeline.queued}
        </span>
      </div>

      <div className="brew-room" ref={tail}>
        {lines.length === 0 ? (
          <p className="brew-empty">{str.pipeline.listening}</p>
        ) : (
          lines.map((line, i) => {
            const newest = i === lines.length - 1;
            if (line.kind === "say") {
              return (
                <div key={line.id} className="brew-msg">
                  <span className="brew-who">{short(line.agent)}</span>
                  <p className="brew-text">
                    <TypedText text={line.text} active={newest} />
                  </p>
                  {!line.usedLlm && <span className="brew-tag rules">{str.pipeline.byRules}</span>}
                </div>
              );
            }
            if (line.kind === "ask") {
              // An agent can address itself -- it is the one that owns the
              // tool it wants run again. "Geospatial -> Geospatial" reads as
              // a rendering bug, so a self-directed request is shown as what
              // it actually is: that agent running one more check.
              const self = line.from === line.to;
              return (
                <div key={line.id} className={`brew-ask${line.accepted ? " live" : ""}`}>
                  <span className="brew-route">
                    {short(line.from)}
                    {!self && (
                      <>
                        {" "}
                        <span aria-hidden="true">→</span> {short(line.to)}
                      </>
                    )}
                  </span>
                  <code className="brew-tool">{line.tool}</code>
                  {line.rule && <span className="brew-tag rules">{str.pipeline.required}</span>}
                  {line.reason && (
                    <p className="brew-text dim">
                      <TypedText text={line.reason} active={newest} />
                    </p>
                  )}
                </div>
              );
            }
            return (
              <div key={line.id} className="brew-flag">
                <span className="brew-who">{short(line.agent)}</span>
                <p className="brew-text">
                  <TypedText text={line.text} active={newest} />
                </p>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
