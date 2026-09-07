import { useEffect, useRef, useState } from "react";
import { fetchReplayList, runReplay, type ReplayTrajectory } from "../api/client";
import { fill, str } from "../i18n/strings";
import { CloseIcon } from "./icons";
import { FOCUSABLE } from "./Sheets";

interface Props {
  onClose: () => void;
}

/** Short display form of an ISO timestamp, always in IST: the archive
    contract is ISO-with-offset and a browser-local conversion would shift
    every row by the viewer's timezone. Raw passthrough when unparseable. */
const IST_FMT = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
function shortTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return IST_FMT.format(d).replace(",", "");
}

function verdictTone(verdict: string): string {
  if (verdict === "go") return "high";
  if (verdict === "marginal") return "med";
  return "low";
}

/**
 * Archived-cyclone replay panel. Lists only storms with an on-disk cache
 * (from GET /replay), runs one through the live tools, and renders the
 * trajectory the server returns. Missing fields render as an error
 * sentence, never as placeholder content.
 */
export default function ReplayPanel({ onClose }: Props) {
  const t = str.replay;
  const [events, setEvents] = useState<string[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [result, setResult] = useState<ReplayTrajectory | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const sheetRef = useRef<HTMLElement | null>(null);

  // Focus contract mirrors Sheets: focus in on open, Tab cycles inside.
  useEffect(() => {
    sheetRef.current?.focus();
  }, []);

  useEffect(() => {
    let alive = true;
    fetchReplayList()
      .then((ids) => {
        if (alive) setEvents(ids);
      })
      .catch(() => {
        if (alive) setLoadError(t.loadError);
      });
    return () => {
      alive = false;
    };
  }, [t.loadError]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const root = sheetRef.current;
      if (!root) return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null,
      );
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey && (active === first || !root.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !root.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function run(eventId: string) {
    setRunningId(eventId);
    setRunError(null);
    setResult(null);
    try {
      const data = await runReplay(eventId);
      if (!data.name || !data.place || !data.landfall || !data.authority || !Array.isArray(data.rows)) {
        setRunError(t.loadError);
        return;
      }
      setResult(data);
    } catch (error) {
      setRunError(error instanceof Error ? error.message : String(error));
    } finally {
      setRunningId(null);
    }
  }

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
      <aside ref={sheetRef} tabIndex={-1} className="sheet replay-sheet" role="dialog" aria-label={t.title}>
        <div className="sheet-head">
          <div>
            <h2>{t.title}</h2>
            <p>{t.sub}</p>
          </div>
          <button className="x" onClick={onClose} aria-label={t.close}>
            <CloseIcon />
          </button>
        </div>
        <div className="sheet-body">
          {loadError && <div className="sheet-note">{loadError}</div>}
          {events !== null && events.length === 0 && !loadError && (
            <div className="empty">{t.empty}</div>
          )}
          {events !== null &&
            events.map((id) => (
              <button
                key={id}
                className="sheet-row"
                disabled={runningId !== null}
                onClick={() => void run(id)}
              >
                <div className="row-top">
                  <b style={{ textTransform: "capitalize" }}>{id}</b>
                  <span className="chip mini">{runningId === id ? t.running : t.run}</span>
                </div>
              </button>
            ))}
          {runError && <div className="sheet-note">{runError}</div>}
          {result && (
            <div className="replay-result">
              <div className="replay-event">
                <b>{result.name}</b>
                <span className="row-meta">
                  {result.place} · {shortTime(result.landfall)}
                </span>
                <span className="row-meta">{result.authority}</span>
              </div>
              <p className="replay-summary">
                {result.summary.first_no_go && result.summary.hours_before_landfall != null
                  ? fill(t.noGoAgo, { h: result.summary.hours_before_landfall })
                  : t.neverRed}
                {result.summary.first_breach && (
                  <>
                    <br />
                    {fill(t.firstBreach, { t: shortTime(result.summary.first_breach) })}
                  </>
                )}
              </p>
              {typeof result.warning === "string" && result.warning.length > 0 && (
                <p className="sheet-note">{result.warning}</p>
              )}
              <div className="grid-wrap">
                <div className="grid-scroll" tabIndex={0} role="region" aria-label={t.tableLabel}>
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">{t.cols.time}</th>
                        <th scope="col">{t.cols.wave}</th>
                        <th scope="col">{t.cols.gust}</th>
                        <th scope="col">{t.cols.score}</th>
                        <th scope="col">{t.cols.verdict}</th>
                        <th scope="col">{t.cols.why}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.rows.map((row, i) => (
                        <tr key={`${row.at}-${i}`}>
                          <td className="mono">{shortTime(row.at)}</td>
                          <td className="mono">{row.wave_m ?? "—"}</td>
                          <td className="mono">{row.gust_kn ?? "—"}</td>
                          <td className="mono">
                            {typeof row.score === "number" ? row.score.toFixed(3) : "—"}
                          </td>
                          <td>
                            <span className={`tag ${verdictTone(row.verdict)}`}>{row.verdict}</span>
                          </td>
                          <td className="replay-why">{row.why}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
              <p className="sheet-note">{fill(t.provenance, { authority: result.authority })}</p>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
