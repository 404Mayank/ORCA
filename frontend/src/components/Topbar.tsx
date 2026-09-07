import { useEffect, useState } from "react";
import type { TierSettings } from "../api/client";
import { str } from "../i18n/strings";
import type { Theme } from "../storage";
import { MenuIcon, MoonIcon, SunIcon, TierIcon } from "./icons";

export type FeedState = "live" | "empty" | "unknown";

interface Props {
  settings: TierSettings | null;
  feed: FeedState;
  theme: Theme;
  onOpenSettings: () => void;
  onToggleTheme: () => void;
  onMenu: () => void;
}

/** Clock in IST, ticking every 15 s. Display only. */
function useISTClock(): string {
  const [now, setNow] = useState("--:--");
  useEffect(() => {
    const tick = () => {
      const t = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000);
      setNow(
        `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`,
      );
    };
    tick();
    const id = setInterval(tick, 15000);
    return () => clearInterval(id);
  }, []);
  return now;
}

/**
 * Top bar. The tier pill is a real control (opens settings, where POST
 * /settings moves the actual tier); the feed pill is a real readout from
 * /readiness. No account, no upgrade -- there is no billing or login.
 */
export default function Topbar({ settings, feed, theme, onOpenSettings, onToggleTheme, onMenu }: Props) {
  const t = str.topbar;
  const clock = useISTClock();
  const beacon = feed === "live" ? "" : feed === "empty" ? "bad" : "warn";
  const feedLabel =
    feed === "live" ? t.feedLive : feed === "empty" ? t.feedEmpty : t.feedUnchecked;
  const tierName = settings ? settings.tier : "…";
  const tierSrc = settings
    ? settings.tier_source === "app"
      ? t.tierFromApp
      : t.tierFromEnv
    : "";

  return (
    <header className="topbar">
      <button className="icon-btn menu-btn" onClick={onMenu} aria-label={str.rail.menuOpen} title={str.rail.menuOpen}>
        <MenuIcon />
      </button>
      <button className="pill" onClick={onOpenSettings} aria-label={t.tierOpensSettings} title={t.tierOpensSettings}>
        <TierIcon />
        <span style={{ textTransform: "capitalize" }}>{tierName}</span>
        {tierSrc && <span className="muted">{tierSrc}</span>}
      </button>

      <div className="topbar-right">
        <div className="feed" title={str.sheets.settings.dataSub}>
          <span className={`beacon ${beacon}`} aria-hidden="true" />
          <span>{feedLabel}</span>
          <span style={{ color: "var(--ink-3)" }}>·</span>
          <span>
            {clock} {str.misc.istSuffix}
          </span>
        </div>
        <button className="icon-btn" onClick={onToggleTheme} aria-label={t.themeToggle} title={t.themeToggle}>
          {theme === "light" ? <MoonIcon /> : <SunIcon />}
        </button>
      </div>
    </header>
  );
}
