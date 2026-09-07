import type { Recommendation } from "../types";
import { fill, str } from "../i18n/strings";

interface Props {
  recommendation: Recommendation;
}

/**
 * The evidence trail: every claim, driver, hypothesis and tool call the
 * answer rests on. Same data the old drawer showed, in source-card rows
 * with confidence meters. Renders what the API returned -- never a
 * second, prettier explanation generated after the fact.
 */
export default function EvidencePane({ recommendation: r }: Props) {
  const s = str.side;
  const secs = s.sections;
  const meterTone = (q: number) =>
    q >= 0.9 ? "var(--green)" : q >= 0.75 ? "var(--teal)" : "var(--amber)";

  return (
    <div id="evidencePane" className="pane">
      <p className="ev-intro">{s.evidenceIntro}</p>

      <div className="ev-sec">{secs.verdict}</div>
      {r.verdict ? (
        <div className="src">
          <div className="src-top">
            <b>{r.verdict.value.toUpperCase()}</b>
            <span className="src-kind">{r.verdict.band}</span>
          </div>
          <div className="src-facts">
            <span>score {r.verdict.score}</span>
            <span>computed by {r.verdict.computed_by}</span>
          </div>
          {r.verdict.limiting_driver && (
            <p>limited by {r.verdict.limiting_driver.replace(/_/g, " ")}</p>
          )}
        </div>
      ) : (
        <div className="src">
          <p>{secs.noVerdict}</p>
        </div>
      )}

      {(r.drivers?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.drivers}</div>
          {r.drivers!.map((d) => {
            const e = d.evaluation;
            // A zero threshold would divide by zero: clamp the denominator,
            // the bar then reads full, which a breaching zero-limit is.
            const pct = Math.min(100, (e.observed.max / (e.threshold.value || 1)) * 100);
            return (
              <div className="src" key={d.id}>
                <div className="src-top">
                  <b>{d.id.replace(/_/g, " ")}</b>
                  <span className="src-kind">
                    {e.breaching ? str.thread.tables.overLimit : str.thread.tables.withinLimit}
                  </span>
                </div>
                <div className="src-facts">
                  <span>
                    observed {e.observed.min}–{e.observed.max} {e.observed.unit}
                  </span>
                  <span>
                    limit {e.threshold.value} {e.threshold.unit}
                  </span>
                </div>
                <div className="meter">
                  <span className="lbl">limit</span>
                  <span className="track">
                    <span
                      className="fill"
                      style={{
                        width: `${pct}%`,
                        ["--tone" as string]: e.breaching ? "var(--red)" : "var(--green)",
                      }}
                    />
                  </span>
                </div>
                <div className="src-facts">
                  <span>{e.threshold.source}</span>
                </div>
              </div>
            );
          })}
        </>
      )}

      {(r.claims?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.claims}</div>
          {r.claims!.map((c) => (
            <div className="src" key={c.id}>
              <div className="src-top">
                <b>{fill(c.template, c.slots)}</b>
                <span className="src-kind">{c.kind}</span>
              </div>
              {(c.evidence?.length ?? 0) > 0 && (
                <div className="src-facts">
                  <span>evidence: {c.evidence!.join(", ")}</span>
                </div>
              )}
            </div>
          ))}
        </>
      )}

      {(r.hypotheses?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.hypotheses}</div>
          {r.hypotheses!.map((h) => (
            <div className="src" key={h.id}>
              <div className="src-top">
                <b>{fill(h.template, h.slots)}</b>
                <span className="src-kind">
                  {h.supported ? str.thread.tables.supported : str.thread.tables.refuted}
                </span>
              </div>
              {h.test_description && <p>{h.test_description}</p>}
              <div className="src-facts">
                <span>tested by {h.tested_by}</span>
              </div>
            </div>
          ))}
        </>
      )}

      {(r.negative_findings?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.checked}</div>
          {r.negative_findings!.map((f) => (
            <div className="src" key={f.id}>
              <div className="src-top">
                <b>{fill(f.template, f.slots)}</b>
                {f.authority && <span className="src-kind">{f.authority}</span>}
              </div>
              <div className="src-facts">
                <span>checked by {f.checked_by ?? "—"}</span>
                {f.valid_until && <span>valid until {f.valid_until}</span>}
              </div>
            </div>
          ))}
        </>
      )}

      {(r.assumptions?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.assumptions}</div>
          {r.assumptions!.map((a, i) => (
            <div className="src" key={i}>
              <p>{a.text}</p>
            </div>
          ))}
        </>
      )}

      {(r.caveats?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.caveats}</div>
          {r.caveats!.map((c, i) => (
            <div className="src" key={i}>
              <p>{c.text}</p>
            </div>
          ))}
        </>
      )}

      {r.confidence && (
        <>
          <div className="ev-sec">{secs.confidence}</div>
          <div className="src">
            <div className="meter">
              <span className="lbl">overall</span>
              <span className="track">
                <span
                  className="fill"
                  style={{
                    width: `${(r.confidence.overall ?? 0) * 100}%`,
                    ["--tone" as string]: meterTone(r.confidence.overall ?? 0),
                  }}
                />
              </span>
              <span className="lbl">{((r.confidence.overall ?? 0) * 100).toFixed(0)}%</span>
            </div>
            <p>{r.confidence.basis}</p>
          </div>
        </>
      )}

      <div className="ev-sec">{fill(secs.toolCalls, { n: r.evidence?.length ?? 0 })}</div>
      {(r.evidence ?? []).map((e) => (
        <div className="src" key={e.tool_call_id}>
          <div className="src-top">
            <b>
              {e.tool_call_id} · {e.tool}
            </b>
            {e.status && <span className="src-kind">{e.status}</span>}
          </div>
          <div className="src-facts">
            <span>{e.source}</span>
            {e.retrieved_at && <span>retrieved {e.retrieved_at}</span>}
            {e.issued_at && <span>issued {e.issued_at}</span>}
            {e.data_age_days != null && <span>{e.data_age_days} d old</span>}
            {e.clear_pass_fraction != null && (
              <span>clear {Math.round(e.clear_pass_fraction * 100)}%</span>
            )}
            {e.composite_window_days != null && (
              <span>{e.composite_window_days}d composite</span>
            )}
          </div>
          {e.error && <p>{e.error}</p>}
        </div>
      ))}

      {(r.reasoning_trace?.length ?? 0) > 0 && (
        <>
          <div className="ev-sec">{secs.trace}</div>
          {(r.reasoning_trace ?? []).map((step, i) => (
            <div className="src" key={i}>
              <div className="src-top">
                <b>
                  {step.step} · {step.tool}
                </b>
                <span className="src-kind">{step.status}</span>
              </div>
              {step.note && <p>{step.note}</p>}
            </div>
          ))}
        </>
      )}

      <p className="ev-foot">{s.evidenceFoot}</p>
    </div>
  );
}
