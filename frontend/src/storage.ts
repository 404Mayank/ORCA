/**
 * Client-side persistence. Everything here is device-local honesty:
 * - theme / locale: pure preferences, applied instantly.
 * - recents: queries this browser sent (the planner never sees them).
 * - saved: answers the user explicitly kept, with the recommendation object
 *   so evidence and map still open. Capped so localStorage never overflows.
 */

export type Theme = "light" | "dark";

const THEME_KEY = "orca-theme";
const RECENTS_KEY = "orca-recents";
const SAVED_KEY = "orca-saved";

export function loadTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** Apply to <html data-theme> so CSS tokens switch. Default is light. */
export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* private mode -- the attribute still applies for this visit */
  }
}

export interface RecentQuery {
  query: string;
  verdict: string | null;
  at: number;
}

export function loadRecents(): RecentQuery[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENTS_KEY) ?? "[]") as RecentQuery[];
    return Array.isArray(raw) ? raw.slice(0, 12) : [];
  } catch {
    return [];
  }
}

export function pushRecent(entry: RecentQuery): RecentQuery[] {
  const list = [entry, ...loadRecents().filter((r) => r.query !== entry.query)].slice(0, 12);
  try {
    localStorage.setItem(RECENTS_KEY, JSON.stringify(list));
  } catch {
    /* storage full or blocked -- recents are a convenience, not data */
  }
  return list;
}

export interface SavedAnswer {
  id: string;
  query: string;
  answer: string;
  verdict: string | null;
  at: number;
  recommendation: Record<string, unknown> | null;
  // Audit stamp, so a re-opened answer keeps its verification state
  // instead of reading as unverified.
  verified: boolean | null;
  numbers_checked: number;
  narration_source: string;
  degraded: boolean;
  duration_ms: number;
}

export function loadSaved(): SavedAnswer[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SAVED_KEY) ?? "[]") as SavedAnswer[];
    return Array.isArray(raw) ? raw.slice(0, 20) : [];
  } catch {
    return [];
  }
}

function storeSaved(list: SavedAnswer[]): void {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(list.slice(0, 20)));
  } catch {
    /* quota -- keep the in-memory list the caller already holds */
  }
}

export function saveAnswer(entry: SavedAnswer): void {
  storeSaved([entry, ...loadSaved().filter((s) => s.id !== entry.id)]);
}

export function removeSaved(id: string): void {
  storeSaved(loadSaved().filter((s) => s.id !== id));
}

