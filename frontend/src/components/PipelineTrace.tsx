import { useEffect, useState } from "react";
import { fill, str } from "../i18n/strings";
import type { StreamEvent } from "../api/client";

/**
 * The brewing card. Renders ONLY what the backend's ProgressBus reported:
 * the deterministic stage roadmap (mirrors orchestrator/progress.py
 * STAGES -- showing the plan ahead is honest, the sequence is fixed),
 * agent chips from deliberate events, and the latest agent assessment
 * VERBATIM (it is number-stripped at its source; CSS ellipsis is the only
 * truncation). No percentages, no ETA, no invented activity -- the only
 * live number is the client-side elapsed clock.
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

interface Props {
  events: StreamEvent[];
  /** Bridge variant: tighter chrome under the composer. */
  compact?: boolean;
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
  // ZERO events (planner still queued -- the common free-tier gap) the
  // honest state is every stage pending: nothing has happened yet.
  const lastStage = last ? (last.stage === "done" ? "narrate" : last.stage) : null;
  const lastIdx = lastStage ? ROADMAP.indexOf(lastStage as (typeof ROADMAP)[number]) : -1;

  // Agents seen in deliberate events, first-seen order; the most recent
  // deliberate event's agent is the lit one. Chip label trims the literal
  // "Agent" suffix (RiskAgent -> Risk) -- a fixed suffix strip, no data
  // invented.
  const agents: string[] = [];
  let latestAssessment = "";
  for (const e of events) {
    if (e.stage === "deliberate" && typeof e.agent === "string" && e.agent) {
      if (!agents.includes(e.agent)) agents.push(e.agent);
    }
    if (e.stage === "deliberate" && typeof e.assessment === "string" && e.assessment) {
      latestAssessment = e.assessment;
    }
  }
  const activeAgent =
    last?.stage === "deliberate" && typeof last.agent === "string" ? last.agent : null;

  return (
    <div className={`brew${compact ? " compact" : ""}`} role="status" aria-live="polite">
      <div className="brew-bar" aria-hidden="true" />
      <div className="brew-head">
        <span className="beacon" aria-hidden="true" />
        <span className="brew-title">{str.pipeline.brewing}</span>
        <span className="brew-elapsed">{fill(str.pipeline.elapsed, { n: String(elapsed) })}</span>
      </div>
      <ol className="brew-stages">
        {ROADMAP.map((stage, i) => (
          <li
            key={stage}
            className={`brew-stage${i < lastIdx ? " done" : i === lastIdx ? " active" : " todo"}`}
          >
            <span className="brew-mark" aria-hidden="true" />
            <span className="brew-stage-name">{str.pipeline.stages[stage] ?? stage}</span>
          </li>
        ))}
      </ol>
      {agents.length > 0 && (
        <div className="brew-agents">
          {agents.map((a) => (
            <span key={a} className={`brew-agent${a === activeAgent ? " active" : ""}`}>
              {a.replace(/Agent$/, "")}
            </span>
          ))}
        </div>
      )}
      {latestAssessment && <p className="brew-say">{latestAssessment}</p>}
    </div>
  );
}
