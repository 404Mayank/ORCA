import { useEffect, useRef } from "react";
import type { SessionTurn, TierSettings } from "../api/client";
import { fill, str } from "../i18n/strings";
import type { RecentQuery, SavedAnswer, Theme } from "../storage";
import type { Locale } from "../i18n/strings";
import { CloseIcon } from "./icons";

export type SheetKey = "conversations" | "saved" | "alerts" | "settings";

export interface LayerStatus {
  cached: boolean;
  age_hours?: number | null;
  observation_age_days?: number | null;
  in_force?: number | null;
  retrieved_at?: string | null;
  stale?: boolean | null;
  max_age_hours?: number | null;
  max_age_days?: number | null;
  operator_checked?: boolean | null;
  operator_age_hours?: number | null;
  operator_max_age_hours?: number | null;
  operator_reviewed_at?: string | null;
}

interface Props {
  sheet: SheetKey;
  onClose: () => void;
  turns: SessionTurn[];
  recents: RecentQuery[];
  onReask: (query: string) => void;
  onNewSession: () => void;
  saved: SavedAnswer[];
  onOpenSaved: (entry: SavedAnswer) => void;
  onRemoveSaved: (id: string) => void;
  /** turn_ids whose full answer is in the current transcript (openable). */
  answerTurnIds: string[];
  onOpenTurn: (turnId: string) => void;
  busy: boolean;
  alerts: LayerStatus | null;
  onAskAlerts: () => void;
  settings: TierSettings | null;
  settingsBusy: boolean;
  onSetTier: (tier: string | null) => void;
  onSetDeliberating: (value: boolean) => void;
  onSetContextTtl: (minutes: number) => void;
  onSetTemplateFallback: (value: boolean) => void;
  onSetMaxRounds: (rounds: number) => void;
  onResetKnobs: () => void;
  theme: Theme;
  onSetTheme: (theme: Theme) => void;
  locale: Locale;
  onSetLocale: (locale: Locale) => void;
  layers: Record<string, LayerStatus>;
  /** Backend recovery hint (e.g. empty-cache fix). Shown verbatim. */
  hint: string | null;
  /** aria-label of the opener to refocus on unmount. Null = leave focus. */
  returnFocusLabel: string | null;
}

const fmtTime = (iso: string) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
};

/**
 * Rail sheets. Each one is backed by something real: the session history
 * endpoint, localStorage the user filled, the alerts cache, or POST
 * /settings. A sheet with nothing to show says so instead of decorating.
 */
export const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export default function Sheets(props: Props) {
  const { sheet, onClose, returnFocusLabel } = props;
  const sh = str.sheets;

  // Return focus to the opener on unmount. Best-effort: a missing label
  // simply leaves focus where the trap last held it (inside, then body).
  useEffect(() => {
    return () => {
      if (!returnFocusLabel) return;
      const opener = document.querySelector<HTMLElement>(
        `[aria-label="${returnFocusLabel}"]`,
      );
      opener?.focus({ preventScroll: true });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const sheetRef = useRef<HTMLElement | null>(null);

  // Focus in on open; Tab cycles inside while open; Escape closes.
  useEffect(() => {
    sheetRef.current?.focus();
  }, [sheet]);

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
        // Focus lost to body (unmounted button, newly disabled control):
        // pull it back in rather than leaking Tab into the background.
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
      <aside
        ref={sheetRef}
        tabIndex={-1}
        className="sheet"
        role="dialog"
        aria-label={sh[sheet].title}
      >
        <div className="sheet-head">
          <div>
            <h2>{sh[sheet].title}</h2>
            {"sub" in sh[sheet] && <p>{(sh[sheet] as { sub: string }).sub}</p>}
          </div>
          <button className="x" onClick={onClose} aria-label={sh.close}>
            <CloseIcon />
          </button>
        </div>
        <div className="sheet-body">
          {sheet === "conversations" && (
            <ConversationsPane {...props} />
          )}
          {sheet === "saved" && <SavedPane {...props} />}
          {sheet === "alerts" && <AlertsPane {...props} />}
          {sheet === "settings" && <SettingsPane {...props} />}
        </div>
      </aside>
    </>
  );
}

function ConversationsPane({ turns, recents, onReask, onNewSession, answerTurnIds, onOpenTurn, busy }: Props) {
  const c = str.sheets.conversations;
  // The history endpoint should return arrays, but a sheet must never be
  // the thing that blanks the app -- coerce defensively, the boundary
  // behind this catches anything stranger.
  const safeTurns = (Array.isArray(turns) ? turns : []).filter((t) => t && typeof t.query === "string");
  const safeRecents = (Array.isArray(recents) ? recents : []).filter(
    (r) => r && typeof r.query === "string",
  );
  return (
    <>
      {safeTurns.length === 0 && safeRecents.length === 0 && <div className="empty">{c.empty}</div>}
      {safeTurns.map((t, i) => {
        // The transcript on screen already holds this answer: open it in
        // place, no API call. Otherwise the only honest action is asking
        // again -- labelled as such, and held while a question is flying.
        const openable = t.turn_id != null && answerTurnIds.includes(t.turn_id);
        return (
          <button
            key={t.turn_id ?? i}
            className="sheet-row"
            disabled={!openable && busy}
            onClick={() => (openable ? onOpenTurn(t.turn_id) : onReask(t.query))}
          >
            <div className="row-top">
              <b>{t.query}</b>
              <span className="chip mini">{openable ? str.sheets.openAction : str.sheets.reaskAction}</span>
            </div>
            <span className="row-meta">
              {t.state ?? "—"}
              {t.verdict ? ` · ${t.verdict}` : ""} · {t.created_at ? fmtTime(t.created_at) : "—"}
            </span>
          </button>
        );
      })}
      {safeRecents.length > 0 && (
        <>
          {safeRecents
            .filter((r) => !safeTurns.some((t) => t.query === r.query))
            .map((r, i) => (
              <button key={`${r.at ?? i}-${r.query}`} className="sheet-row" disabled={busy} onClick={() => onReask(r.query)}>
                <div className="row-top">
                  <b>{r.query}</b>
                  <span className="chip mini">{str.sheets.reaskAction}</span>
                </div>
                <span className="row-meta">
                  {r.verdict ?? "—"} ·{" "}
                  {typeof r.at === "number" ? new Date(r.at).toLocaleString() : "—"}
                </span>
              </button>
            ))}
        </>
      )}
      <div className="sheet-note">
        <button className="chip" onClick={onNewSession}>
          {c.newSession}
        </button>
      </div>
    </>
  );
}

function SavedPane({ saved, onOpenSaved, onRemoveSaved }: Props) {
  const c = str.sheets.saved;
  if (saved.length === 0) return <div className="empty">{c.empty}</div>;
  return (
    <>
      {saved.map((s) => (
        <div key={s.id} className="sheet-row">
          <div className="row-top">
            <b>{s.query}</b>
          </div>
          <span className="row-meta">
            {s.verdict ?? "—"} · {new Date(s.at).toLocaleString()}
          </span>
          <span className="seg-row">
            <button className="chip" onClick={() => onOpenSaved(s)}>
              {c.open}
            </button>
            <button className="chip" onClick={() => onRemoveSaved(s.id)}>
              {c.remove}
            </button>
          </span>
        </div>
      ))}
    </>
  );
}

function AlertsPane({ alerts, onAskAlerts }: Props) {
  const c = str.sheets.alerts;
  const cached = alerts?.cached === true;
  const inForce = alerts?.in_force ?? null;
  // The cyclone cache (GDACS) and the operator wave table go stale on
  // different clocks. A fresh cyclone negative finding stands on its own,
  // but it must not read as a full all-clear when the wave half is overdue.
  const waveStale = alerts?.operator_checked === false;
  return (
    <>
      {cached && inForce != null && inForce > 0 && (
        <div className="sheet-row">
          <b>{fill(c.inForce, { n: inForce, s: inForce === 1 ? "y" : "ies" })}</b>
          <small>{alerts?.retrieved_at ? fmtTime(alerts.retrieved_at) : ""}</small>
        </div>
      )}
      {cached && (inForce ?? -1) === 0 && (
        <div className="sheet-note">
          {fill(c.noneChecked, { at: alerts?.retrieved_at ? fmtTime(alerts.retrieved_at!) : "—" })}
        </div>
      )}
      {!cached && <div className="sheet-note">{c.unchecked}</div>}
      {waveStale && (
        <div className="sheet-note">
          {alerts?.operator_age_hours != null
            ? fill(c.waveStale, {
                n: Math.round(alerts.operator_age_hours),
                m: alerts?.operator_max_age_hours ?? "—",
              })
            : c.waveUnchecked}
        </div>
      )}
      <div className="sheet-note">
        <button className="chip" onClick={onAskAlerts}>
          {c.askAbout}
        </button>
      </div>
      <div className="sheet-note">{str.meta.disclaimer}</div>
    </>
  );
}

const MEMORY_PRESETS = [30, 90, 180];
const ROUNDS_PRESETS = [0, 1, 2];

function SettingsPane({ settings, settingsBusy, onSetTier, onSetDeliberating, onSetContextTtl, onSetTemplateFallback, onSetMaxRounds, onResetKnobs, theme, onSetTheme, locale, onSetLocale, layers, hint }: Props) {
  const c = str.sheets.settings;
  const tierCopy = c.tiers;
  return (
    <>
      <div className="ev-sec">{c.tierHeading}</div>
      <div className="sheet-note">{c.tierSub}</div>
      {(settings?.tiers ?? ["free", "fast", "paid"]).map((id) => (
        <div key={id} style={{ padding: "0 8px 6px" }}>
          <button
            className="tier-pick"
            aria-pressed={settings?.tier === id}
            disabled={settingsBusy}
            onClick={() => onSetTier(id)}
          >
            <b>
              {(tierCopy[id] ?? { name: id }).name}
            </b>
            <small>{(tierCopy[id] ?? { desc: "" }).desc}</small>
            {settings?.tier === id && (
              <span className="row-meta">
                {settings.tier_source === "app"
                  ? str.topbar.tierFromApp
                  : settings.tier_source === "env"
                    ? str.topbar.tierFromEnv
                    : str.topbar.tierDefault}
              </span>
            )}
          </button>
        </div>
      ))}
      <div style={{ padding: "0 8px 6px" }}>
        <button className="tier-pick" disabled={settingsBusy || settings?.tier_source !== "app"} onClick={() => onSetTier(null)}>
          <b>{c.useEnv}</b>
          <small>{c.useEnvSub} (ORCA_TIER)</small>
        </button>
      </div>

      <div className="ev-sec">{c.reasoningHeading}</div>
      <div className="sheet-note">{c.reasoningSub}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {[true, false].map((value) => (
          <button
            key={String(value)}
            className="tier-pick"
            aria-pressed={(settings?.deliberating ?? true) === value}
            disabled={settingsBusy || settings == null}
            onClick={() => onSetDeliberating(value)}
          >
            <b>{value ? c.reasoningOn : c.reasoningOff}</b>
          </button>
        ))}
      </div>

      <div className="ev-sec">{c.memoryHeading}</div>
      <div className="sheet-note">{c.memorySub}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {MEMORY_PRESETS.map((minutes, i) => (
          <button
            key={minutes}
            className="tier-pick"
            aria-pressed={(settings?.context_ttl_min ?? 90) === minutes}
            disabled={settingsBusy || settings == null}
            onClick={() => onSetContextTtl(minutes)}
          >
            <b>{c.memoryPresets[i] ?? `${minutes} min`}</b>
          </button>
        ))}
      </div>

      <div className="ev-sec">{c.fallbackHeading}</div>
      <div className="sheet-note">{c.fallbackSub}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {[true, false].map((value) => (
          <button
            key={String(value)}
            className="tier-pick"
            aria-pressed={(settings?.template_fallback ?? true) === value}
            disabled={settingsBusy || settings == null}
            onClick={() => onSetTemplateFallback(value)}
          >
            <b>{value ? c.fallbackOn : c.fallbackOff}</b>
          </button>
        ))}
      </div>

      <div className="ev-sec">{c.roundsHeading}</div>
      <div className="sheet-note">{c.roundsSub}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {ROUNDS_PRESETS.map((rounds) => (
          <button
            key={rounds}
            className="tier-pick"
            aria-pressed={(settings?.max_rounds ?? 2) === rounds}
            disabled={settingsBusy || settings == null}
            onClick={() => onSetMaxRounds(rounds)}
          >
            <b>{rounds === 0 ? c.roundsNone : rounds === 1 ? c.roundsOne : fill(c.roundsMany, { n: rounds })}</b>
          </button>
        ))}
      </div>

      <div style={{ padding: "0 8px 6px" }}>
        <button
          className="tier-pick"
          disabled={settingsBusy || settings == null}
          onClick={() => onResetKnobs()}
        >
          <b>{c.resetAll}</b>
          <small>{c.resetAllSub}</small>
        </button>
      </div>

      <div className="ev-sec">{c.appearanceHeading}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {(["light", "dark"] as Theme[]).map((id) => (
          <button
            key={id}
            className="tier-pick"
            aria-pressed={theme === id}
            onClick={() => onSetTheme(id)}
          >
            <b>{id === "light" ? c.light : c.dark}</b>
          </button>
        ))}
      </div>

      <div className="ev-sec">{c.languageHeading}</div>
      <div className="sheet-note">{c.tamilNote}</div>
      <div style={{ padding: "0 8px 6px", display: "flex", flexDirection: "column", gap: 6 }}>
        {(["en", "ta"] as Locale[]).map((id) => (
          <button
            key={id}
            className="tier-pick"
            aria-pressed={locale === id}
            onClick={() => onSetLocale(id)}
          >
            <b>{id === "en" ? c.english : c.tamil}</b>
          </button>
        ))}
      </div>

      <div className="ev-sec">{c.dataHeading}</div>
      <div className="sheet-note">{c.dataSub}</div>
      {hint && <div className="sheet-note mono-note">{hint}</div>}
      {Object.entries(layers).map(([name, layer]) => {
        // One joined sub-line: no stray separators, and no empty row when
        // a layer carries nothing but a state (uncached ocean layers have
        // no age and no count).
        const parts: string[] = [];
        if (layer.age_hours != null) {
          // Sub-hour ages read as minutes: rounding 24 min to "0 h old"
          // looks fresher than it is, and one decimal under 10 h keeps a
          // 90-minute-old cache from reading as either 1 or 2 hours.
          const h = layer.age_hours;
          parts.push(
            h < 1
              ? fill(c.ageMinutes, { n: Math.max(1, Math.round(h * 60)) })
              : fill(c.ageHours, { n: h < 10 ? Math.round(h * 10) / 10 : Math.round(h) }),
          );
        }
        if (layer.observation_age_days != null) {
          const days = Math.round(layer.observation_age_days * 10) / 10;
          parts.push(fill(c.ageDays, { n: days }));
        }
        if (layer.in_force != null) parts.push(fill(c.inForce, { n: layer.in_force }));
        if (name === "alerts") {
          // This row has only ever been the GDACS cyclone check. Say so,
          // and say whether the operator wave table behind high-wave /
          // swell-surge answers is itself fresh -- a fresh cyclone cache
          // must not mask a stale wave table.
          parts.push(c.alertsScope);
          if (layer.operator_checked === true) parts.push(c.waveTableChecked);
          else if (layer.operator_checked === false) {
            parts.push(
              layer.operator_age_hours != null
                ? fill(c.waveTableStale, { n: Math.round(layer.operator_age_hours) })
                : c.waveTableUnchecked,
            );
          }
        }
        // A stale layer still has a file on disk, but the file is too old
        // to support a verdict: it renders as stale, never as healthy.
        const state = layer.stale ? c.stale : layer.cached ? c.cached : c.missing;
        return (
          <div className="sheet-row" key={name}>
            <div className="row-top">
              <b>{c.layerNames[name] ?? name}</b>
              <span className="row-meta">{state}</span>
            </div>
            {parts.length > 0 && <span className="row-meta">{parts.join(" · ")}</span>}
          </div>
        );
      })}
    </>
  );
}
