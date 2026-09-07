import { fill, str } from "../i18n/strings";
import {
  BellIcon,
  BrandMark,
  ChatsIcon,
  GearIcon,
  HomeIcon,
  NewIcon,
  SavedIcon,
  StormIcon,
} from "./icons";

export type RailKey = "bridge" | "new" | "conversations" | "saved" | "alerts" | "replay" | "settings";

interface Props {
  active: RailKey | null;
  /** Real advisory count from the alerts cache. Null while unchecked. */
  alertBadge: number | null;
  alertsCached: boolean;
  onNav: (key: RailKey) => void;
  /** Phone drawer state. Desktop ignores it; the rail is always visible. */
  open: boolean;
  onClose: () => void;
}

/**
 * The left rail. Every item does something real:
 * bridge/new navigate, the rest open sheets backed by live state.
 * Nothing decorative ships -- a dead icon that only looks like a
 * product is worse than a missing one.
 */
export default function Rail({ active, alertBadge, alertsCached, onNav, open, onClose }: Props) {
  const r = str.rail;
  return (
    <>
    {open && (
      <button className="rail-backdrop" aria-label={r.menuClose} onClick={onClose} />
    )}
    <aside className={`rail${open ? " open" : ""}`}>
      <div className="brand">
        <div className="brand-mark" aria-hidden="true">
          <BrandMark />
        </div>
        <div className="brand-name">{str.meta.appName}</div>
        <div className="brand-sub">{str.meta.appSub}</div>
      </div>

      <nav className="rail-group" aria-label={str.workspaceNav}>
        <button
          className="rail-btn"
          aria-current={active === "bridge" ? "page" : undefined}
          aria-label={r.bridge.label}
          onClick={() => onNav("bridge")}
        >
          <HomeIcon />
          <span className="tip">{r.bridge.tip}</span>
        </button>
        <button className="rail-btn" aria-label={r.newer.label} onClick={() => onNav("new")}>
          <NewIcon />
          <span className="tip">{r.newer.tip}</span>
        </button>
        <button
          className="rail-btn"
          aria-current={active === "conversations" ? "page" : undefined}
          aria-label={r.conversations.label}
          onClick={() => onNav("conversations")}
        >
          <ChatsIcon />
          <span className="tip">{r.conversations.tip}</span>
        </button>
        <button
          className="rail-btn"
          aria-current={active === "saved" ? "page" : undefined}
          aria-label={r.saved.label}
          onClick={() => onNav("saved")}
        >
          <SavedIcon />
          <span className="tip">{r.saved.tip}</span>
        </button>
        <div className="rail-rule" aria-hidden="true" />
        <button
          className="rail-btn"
          aria-current={active === "alerts" ? "page" : undefined}
          aria-label={
            alertBadge != null && alertBadge > 0
              ? fill(r.alertsActive, { n: alertBadge })
              : r.alerts.label
          }
          onClick={() => onNav("alerts")}
        >
          <BellIcon />
          {alertBadge != null && alertBadge > 0 && <span className="dot">{alertBadge}</span>}
          {alertsCached === false && <span className="dot warn">!</span>}
          <span className="tip">
            {alertBadge != null && alertBadge > 0 ? fill(r.alertsActive, { n: alertBadge }) : r.alerts.tip}
          </span>
        </button>
        {alertBadge != null && alertBadge > 0 && (
          <p className="rail-caption">
            {alertBadge} {r.alerts.caption}
          </p>
        )}
        <button
          className="rail-btn"
          aria-current={active === "replay" ? "page" : undefined}
          aria-label={r.replay.label}
          onClick={() => onNav("replay")}
        >
          <StormIcon />
          <span className="tip">{r.replay.tip}</span>
        </button>
      </nav>

      <div className="rail-group bottom">
        <button
          className="rail-btn"
          aria-current={active === "settings" ? "page" : undefined}
          aria-label={r.settings.label}
          onClick={() => onNav("settings")}
        >
          <GearIcon />
          <span className="tip">{r.settings.tip}</span>
        </button>
      </div>
    </aside>
    </>
  );
}
