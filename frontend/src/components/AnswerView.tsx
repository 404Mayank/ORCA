import type {
  ChatResponse,
  Claim,
  Range,
  Recommendation,
  Window as SeaWindow,
} from "../types";
import { fill, str } from "../i18n/strings";
import { DocIcon } from "./icons";

interface Props {
  response: ChatResponse;
  onOpenEvidence: () => void;
  onSend: (text: string) => void;
  busy?: boolean;
}

const fmtRange = (r: Range) => `${r.min}–${r.max} ${r.unit}`;

function isZoneClaim(c: Claim): boolean {
  const d = Number(c.slots?.["distance_km"]);
  const b = Number(c.slots?.["bearing_deg"]);
  return Number.isFinite(d) && Number.isFinite(b);
}

const fmtTime = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
};

function fmtWindow(w: SeaWindow): string {
  return `${fmtTime(w.opens)} → back by ${fmtTime(w.turn_back)} → ashore ${fmtTime(w.ashore_by)}`;
}

/**
 * One ORCA answer. Prose first (narrated, number-guarded), then the
 * structured blocks read straight off the typed recommendation -- drivers
 * with limits, zones, tested hypotheses, checked-and-clear findings.
 * Nothing here is invented in the browser: a block renders only when the
 * object carries it.
 */
export default function AnswerView({ response, onOpenEvidence, onSend, busy }: Props) {
  const t = str.thread;
  const rec = response.recommendation as Recommendation | null | undefined;
  const verdict = response.verdict;
  const paragraphs = response.answer.split(/\n\n+/).filter((p) => p.trim().length > 0);

  const zoneClaims = (rec?.claims ?? []).filter(isZoneClaim);
  const otherClaims = (rec?.claims ?? []).filter((c) => !isZoneClaim(c));
  const evidenceCount = rec?.evidence?.length ?? 0;
  const assumptions = rec?.assumptions ?? [];
  const guidance = [...(rec?.operational_guidance ?? [])].sort((a, b) => a.priority - b.priority);

  return (
    <div className="answer">
      {verdict && (
        <div className={`verdict-banner ${verdict}`} role="status">
          {t.verdicts[verdict] ?? verdict.toUpperCase()}
          {rec?.verdict?.band && <small>{rec.verdict.band}</small>}
        </div>
      )}

      {paragraphs.map((p, i) => (
        <p key={i}>{p}</p>
      ))}

      {(rec?.drivers?.length ?? 0) > 0 && (
        <>
          <h3>{t.tables.driversHeading}</h3>
          <div className="grid-wrap">
            <div className="grid-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.tables.driver}</th>
                    <th scope="col">{t.tables.observed}</th>
                    <th scope="col">{t.tables.limit}</th>
                    <th scope="col">{t.tables.status}</th>
                  </tr>
                </thead>
                <tbody>
                  {rec!.drivers!.map((d) => (
                    <tr key={d.id}>
                      <td className="lead">{d.id.replace(/_/g, " ")}</td>
                      <td className="mono">{fmtRange(d.evaluation.observed)}</td>
                      <td className="mono">
                        {d.evaluation.threshold.value} {d.evaluation.threshold.unit}
                      </td>
                      <td>
                        <span className={`tag ${d.evaluation.breaching ? "low" : "high"}`}>
                          {d.evaluation.breaching ? t.tables.overLimit : t.tables.withinLimit}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {zoneClaims.length > 0 && (
        <>
          <h3>{t.tables.zonesHeading}</h3>
          <div className="grid-wrap">
            <div className="grid-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">{t.tables.zone}</th>
                    <th scope="col">{t.tables.run}</th>
                    <th scope="col">{t.tables.bearing}</th>
                  </tr>
                </thead>
                <tbody>
                  {zoneClaims.map((c) => (
                    <tr key={c.id}>
                      <td className="lead">{c.id}</td>
                      <td className="mono">{Number(c.slots?.["distance_km"])} km</td>
                      <td className="mono">{Number(c.slots?.["bearing_deg"])}°</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {otherClaims.length > 0 && (
        <>
          <h3>{t.tables.claimsHeading}</h3>
          {otherClaims.map((c) => (
            <p className="note" key={c.id}>
              {fill(c.template, c.slots)}{" "}
              <span className={`tag ${c.kind === "observed" ? "high" : c.kind === "derived" ? "med" : "neutral"}`}>
                {c.kind}
              </span>
            </p>
          ))}
        </>
      )}

      {(rec?.hypotheses?.length ?? 0) > 0 && (
        <>
          <h3>{t.tables.hypothesesHeading}</h3>
          {rec!.hypotheses!.map((h) => (
            <p className="note" key={h.id}>
              {fill(h.template, h.slots)}{" "}
              <span className={`tag ${h.supported ? "high" : "low"}`}>
                {h.supported ? t.tables.supported : t.tables.refuted}
              </span>
              {h.test_description && (
                <>
                  <br />
                  <span>{h.test_description}</span>
                </>
              )}
            </p>
          ))}
          <p className="note">{t.hypothesesNote}</p>
        </>
      )}

      {(rec?.negative_findings?.length ?? 0) > 0 && (
        <>
          <h3>{t.tables.checkedHeading}</h3>
          {rec!.negative_findings!.map((f) => (
            <p className="note" key={f.id}>
              {fill(f.template, f.slots)}
            </p>
          ))}
        </>
      )}

      {rec?.window && (
        <>
          <h3>{t.tables.windowHeading}</h3>
          <p className="note">{fmtWindow(rec.window)}</p>
        </>
      )}

      {(rec?.alternatives?.length ?? 0) > 0 && (
        <>
          <h3>{t.tables.alternativesHeading}</h3>
          {rec!.alternatives!.map((a) => (
            <p className="note" key={a.id}>
              {fill(a.template, a.slots)} (+{a.cost.extra_distance_km} km
              {a.cost.extra_steam_time_h != null && `, +${a.cost.extra_steam_time_h} h`}
              {a.cost.note && ` — ${a.cost.note}`})
            </p>
          ))}
        </>
      )}

      {guidance.length > 0 && (
        <>
          <h3>{t.tables.guidanceHeading}</h3>
          {guidance.map((g, i) => (
            <p className="note" key={i}>
              {fill(g.template, g.slots)}
            </p>
          ))}
        </>
      )}

      {assumptions.length > 0 && (
        <p className="note">
          {t.assumptions}:{" "}
          {assumptions
            .map((a) =>
              a.field && a.value != null ? `${a.text} (${a.field}: ${a.value})` : a.text,
            )
            .join(" · ")}
        </p>
      )}

      {rec?.refusal && (
        <>
          <h3>{t.refusalHeading}</h3>
          <p className="note">{fill(rec.refusal.explanation_template, rec.refusal.slots)}</p>
          {rec.refusal.supported_region && (
            <p className="note">{rec.refusal.supported_region}</p>
          )}
        </>
      )}

      {evidenceCount > 0 && (
        <button className="cited" onClick={onOpenEvidence}>
          <DocIcon />
          {fill(t.cited, { n: evidenceCount })}
        </button>
      )}

      <div className="meta">
        {response.verified === true && (
          <span>{fill(t.verified, { n: response.numbers_checked })}</span>
        )}
        {response.verified === false && <span>{t.verifyFailed}</span>}
        {response.degraded && <span>{t.degraded}</span>}
        {response.used_fallback_plan && <span>{t.fallbackPlan}</span>}
        {response.narration_source && <span>{response.narration_source}</span>}
        {response.llm_provider && response.llm_provider !== "none" && (
          <span>{response.llm_provider}</span>
        )}
        <span>{response.duration_ms} ms</span>
      </div>

      {response.state === "clarification" && response.missing_slots && response.missing_slots.length > 0 && (
        <p className="note">{fill(t.needsSlots, { list: response.missing_slots.join(", ") })}</p>
      )}
      {response.options && response.options.length > 0 && (
        <div className={`options${response.state === "chat" ? " suggest" : ""}`}>
          {response.options.map((option) => (
            <button key={option} disabled={busy} onClick={() => onSend(option)}>
              {/* Display is humanized; the click still sends the raw value
                  verbatim, which is what the slot gate matches on. */}
              {response.state === "chat" ? option : option.replace(/_/g, " ")}
            </button>
          ))}
        </div>
      )}

      {response.agent_reasoning && response.agent_reasoning.length > 0 && (
        <div className="collab">
          <div className="collab-h">{t.agentsReasoned}</div>
          {response.agent_reasoning.map((line, n) => (
            <div className="collab-line" key={n}>
              {line}
            </div>
          ))}
        </div>
      )}

      {response.collaboration && response.collaboration.length > 0 && (
        <div className="collab">
          <div className="collab-h">
            {fill(t.collaborated, {
              n: response.collaboration_rounds,
              s: response.collaboration_rounds === 1 ? "" : "s",
            })}
          </div>
          {response.collaboration.map((line, n) => (
            <div className="collab-line" key={n}>
              {line}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
