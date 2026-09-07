import { str } from "../i18n/strings";
import type { StreamEvent } from "../api/client";

interface Props {
  events: StreamEvent[];
}

/**
 * The live pipeline trace. Renders ONLY stages the backend reported --
 * never percentages, never invented activity. Collapses away once the
 * answer lands (the verified meta line takes over as the audit surface).
 */
export default function PipelineTrace({ events }: Props) {
  const labels = str.pipeline.stages;
  const shown = events.slice(-8);
  return (
    <div className="pipeline" role="status" aria-live="polite">
      {shown.map((e) => (
        <div className="pipeline-row" key={e.seq}>
          <span className="beacon" aria-hidden="true" />
          <span className="pipeline-stage">{labels[e.stage] ?? e.stage}</span>
          {typeof e.agent === "string" && e.agent.length > 0 && (
            <span className="pipeline-agent">{e.agent}</span>
          )}
          {typeof e.assessment === "string" && e.assessment.length > 0 && (
            <span className="pipeline-note">{e.assessment}</span>
          )}
        </div>
      ))}
    </div>
  );
}
