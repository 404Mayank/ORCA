import type { Recommendation } from "../types";

/**
 * The explainability drawer.
 *
 * This is the headline feature made visible: every claim, every driver with its
 * threshold, every hypothesis with whether the data supported it, and every
 * tool call the answer rests on. It renders what the API already returned --
 * it does not ask for a second, prettier explanation, because an explanation
 * generated after the fact is not evidence of anything.
 */

interface Props {
  recommendation: Recommendation;
  onClose: () => void;
}

function fill(template: string, slots?: Record<string, unknown> | null): string {
  if (!slots) return template;
  return template.replace(/\{(\w+)\}/g, (whole, key) =>
    slots[key] === undefined || slots[key] === null ? whole : String(slots[key]),
  );
}

export default function EvidenceDrawer({ recommendation, onClose }: Props) {
  const r = recommendation;

  return (
    <div className="drawer">
      <button className="close" onClick={onClose} aria-label="Close evidence">
        ×
      </button>

      <h2>Verdict</h2>
      <div className="card">
        {r.verdict ? (
          <>
            <strong>{r.verdict.value.toUpperCase()}</strong>
            <div className="k">
              score {r.verdict.score} · {r.verdict.band}
            </div>
            <div className="k mono">computed by {r.verdict.computed_by}</div>
          </>
        ) : (
          <span className="k">
            No verdict — this query type does not adjudicate safety.
          </span>
        )}
      </div>

      {(r.drivers?.length ?? 0) > 0 && (
        <>
          <h2>Drivers</h2>
          {r.drivers!.map((d) => {
            const e = d.evaluation;
            return (
              <div className="card" key={d.id}>
                <div>
                  {d.id.replace(/_/g, " ")}
                  <span className={`pill ${e.breaching ? "no" : "yes"}`}>
                    {e.breaching ? "over limit" : "within limit"}
                  </span>
                </div>
                <div className="k">
                  observed {e.observed.min}–{e.observed.max} {e.observed.unit} · limit{" "}
                  {e.threshold.value} {e.threshold.unit}
                </div>
                <div className="bar">
                  <i
                    style={{
                      width: `${Math.min(100, (e.observed.max / e.threshold.value) * 100)}%`,
                      background: e.breaching ? "var(--no-go)" : "var(--go)",
                    }}
                  />
                </div>
                <div className="k mono">{e.threshold.source}</div>
              </div>
            );
          })}
        </>
      )}

      {(r.claims?.length ?? 0) > 0 && (
        <>
          <h2>Claims</h2>
          {r.claims!.map((c) => (
            <div className="card" key={c.id}>
              <div>
                {fill(c.template, c.slots)}
                <span className={`pill ${c.kind}`}>{c.kind}</span>
              </div>
              {(c.evidence?.length ?? 0) > 0 && (
                <div className="k mono">evidence: {c.evidence!.join(", ")}</div>
              )}
            </div>
          ))}
        </>
      )}

      {(r.hypotheses?.length ?? 0) > 0 && (
        <>
          <h2>Hypotheses tested</h2>
          {r.hypotheses!.map((h) => (
            <div className="card" key={h.id}>
              <div>
                {fill(h.template, h.slots)}
                <span className={`pill ${h.supported ? "yes" : "no"}`}>
                  {h.supported ? "supported" : "not supported"}
                </span>
              </div>
              {h.test_description && <div className="k">{h.test_description}</div>}
              <div className="k mono">tested by {h.tested_by}</div>
            </div>
          ))}
        </>
      )}

      {(r.negative_findings?.length ?? 0) > 0 && (
        <>
          <h2>Checked and clear</h2>
          {r.negative_findings!.map((f) => (
            <div className="card" key={f.id}>
              {fill(f.template, f.slots)}
              <div className="k mono">checked by {f.checked_by ?? "—"}</div>
            </div>
          ))}
        </>
      )}

      {(r.assumptions?.length ?? 0) > 0 && (
        <>
          <h2>Assumptions</h2>
          {r.assumptions!.map((a, i) => (
            <div className="card" key={i}>
              {a.text}
            </div>
          ))}
        </>
      )}

      {(r.caveats?.length ?? 0) > 0 && (
        <>
          <h2>Caveats</h2>
          {r.caveats!.map((c, i) => (
            <div className="card" key={i}>
              {c.text}
            </div>
          ))}
        </>
      )}

      <h2>Confidence</h2>
      <div className="card">
        <div>{((r.confidence?.overall ?? 0) * 100).toFixed(0)}%</div>
        <div className="bar">
          <i style={{ width: `${(r.confidence?.overall ?? 0) * 100}%` }} />
        </div>
        <div className="k">{r.confidence?.basis}</div>
      </div>

      <h2>Tool calls ({r.evidence?.length ?? 0})</h2>
      {(r.evidence ?? []).map((e) => (
        <div className="card" key={e.tool_call_id}>
          <div className="mono">
            {e.tool_call_id} · {e.tool}
          </div>
          <div className="k">{e.source}</div>
          {e.retrieved_at && <div className="k">retrieved {e.retrieved_at}</div>}
        </div>
      ))}

      <h2>Reasoning trace</h2>
      {(r.reasoning_trace ?? []).map((s, i) => (
        <div className="card" key={i}>
          <div>
            <span className="mono">{s.step}</span> {s.tool}
            <span className={`pill ${s.status === "failed" ? "no" : "yes"}`}>
              {s.status}
            </span>
          </div>
          {s.note && <div className="k">{s.note}</div>}
        </div>
      ))}
    </div>
  );
}
