import type { ChatRequest, ChatResponse } from "../types";

/**
 * The API client. Paths are same-origin and proxied to uvicorn in dev
 * (see vite.config.ts), so no base URL is baked into the build.
 */

export interface ReadinessLayer {
  cached: boolean;
  retrieved_at?: string | null;
  age_hours?: number | null;
  observation_age_days?: number | null;
  dataset?: string;
  in_force?: number | null;
}

export interface Readiness {
  ready: boolean;
  hint: string | null;
  layers: Record<string, ReadinessLayer>;
  llm_providers: Record<string, boolean>;
  bbox: [number, number, number, number];
}

/** One question. Never throws for a domain failure — the API returns a state. */
export async function ask(request: ChatRequest): Promise<ChatResponse> {
  const response = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    // A non-200 is an infrastructure failure (server down), not a refusal.
    // Refusals and clarifications arrive as 200 with a state, by design.
    throw new Error(`API returned ${response.status}`);
  }
  return response.json();
}

export async function readiness(): Promise<Readiness> {
  const response = await fetch("/readiness");
  if (!response.ok) throw new Error(`readiness ${response.status}`);
  return response.json();
}

export function newSessionId(): string {
  return `s_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

export interface Boundaries {
  imbl: Array<[number, number]>;
  source: string;
  notes: string[];
  bbox: number[];
}

let boundariesCache: Promise<Boundaries> | null = null;

/** Static treaty geometry, fetched once. Same line the geofence tool tests. */
export function getBoundaries(): Promise<Boundaries> {
  if (!boundariesCache) {
    boundariesCache = fetch("/geo/boundaries").then((response) => {
      if (!response.ok) {
        boundariesCache = null;
        throw new Error(`boundaries ${response.status}`);
      }
      return response.json();
    });
  }
  return boundariesCache;
}

export interface TierSettings {
  tier: string;
  tier_source: string;
  tiers: string[];
  provider_order: string[];
  providers: Record<string, boolean>;
  deliberating: boolean;
  context_ttl_min: number;
  template_fallback: boolean;
  max_rounds: number;
}

/** Live tier state. The POST takes effect on the next question, no restart. */
export async function getSettings(): Promise<TierSettings> {
  const response = await fetch("/settings");
  if (!response.ok) throw new Error(`settings ${response.status}`);
  return response.json();
}

async function postSettings(patch: Record<string, unknown>): Promise<TierSettings> {
  const response = await fetch("/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new Error(`settings ${response.status}`);
  return response.json();
}

export async function setTier(tier: string | null): Promise<TierSettings> {
  return postSettings({ tier });
}

export async function setDeliberating(deliberating: boolean): Promise<TierSettings> {
  return postSettings({ deliberating });
}

export async function setContextTtl(context_ttl_min: number): Promise<TierSettings> {
  return postSettings({ context_ttl_min });
}

export async function setTemplateFallback(template_fallback: boolean): Promise<TierSettings> {
  return postSettings({ template_fallback });
}

export async function setMaxRounds(max_rounds: number): Promise<TierSettings> {
  return postSettings({ max_rounds });
}

/** Return the eight tuned knobs to compiled defaults. Tier untouched. */
export async function resetKnobs(): Promise<TierSettings> {
  return postSettings({
    reset: [
      "deliberating",
      "context_ttl_min",
      "pending_ttl_min",
      "max_rounds",
      "max_added_steps",
      "template_fallback",
      "llm_timeout_s",
      "step_timeout_s",
    ],
  });
}

export class StreamFailed extends Error {
  constructor(message = "stream failed") {
    super(message);
    this.name = "StreamFailed";
  }
}

export interface StreamEvent {
  seq: number;
  stage: string;
  [key: string]: unknown;
}

/**
 * Same turn over SSE: stage events while it runs, full ChatResponse in
 * the terminal done frame. Throws StreamFailed on any transport trouble
 * so the caller falls back to blocking ask() -- a partial answer is
 * never surfaced.
 */
export async function askStream(
  request: ChatRequest,
  onEvent: (event: StreamEvent) => void,
): Promise<ChatResponse> {
  let response: Response;
  try {
    response = await fetch("/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify(request),
    });
  } catch {
    throw new StreamFailed("unreachable");
  }
  if (!response.ok || !response.body) throw new StreamFailed(`status ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const pump = async (): Promise<ChatResponse> => {
    for (;;) {
      const { done, value } = await reader.read();
      if (value) buffer += decoder.decode(value, { stream: !done });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const kindEnd = chunk.indexOf("\n");
        const kind = kindEnd >= 0 ? chunk.slice(0, kindEnd).trim() : chunk.trim();
        const dataIdx = chunk.indexOf("data: ");
        if (dataIdx < 0) continue;
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(chunk.slice(dataIdx + 6));
        } catch {
          throw new StreamFailed("bad frame");
        }
        if (kind === "event: stage") {
          onEvent(payload as StreamEvent);
        } else if (kind === "event: done") {
          await reader.cancel().catch(() => {});
          return payload as unknown as ChatResponse;
        }
      }
      if (done) throw new StreamFailed("cut off before done");
    }
  };
  try {
    return await pump();
  } catch (error) {
    if (error instanceof StreamFailed) throw error;
    throw new StreamFailed(String(error));
  }
}

export interface ReplayRow {
  at: string;
  wave_m: number | null;
  gust_kn: number | null;
  vis_km: number | null;
  score: number;
  verdict: string;
  why: string;
}

export interface ReplayTrajectory {
  event: string;
  name: string;
  place: string;
  vessel_class: string;
  landfall: string;
  authority: string;
  note: string;
  replay: boolean;
  warning?: string;
  rows: ReplayRow[];
  summary: {
    first_no_go: string | null;
    hours_before_landfall: number | null;
    first_breach: string | null;
  };
}

/** Archived storm ids with a usable on-disk cache. [] when none. */
export async function fetchReplayList(): Promise<string[]> {
  const response = await fetch("/replay");
  if (!response.ok) throw new Error(`replay ${response.status}`);
  const body: unknown = await response.json();
  if (!Array.isArray(body) || !body.every((e) => typeof e === "string")) {
    throw new Error("replay list malformed");
  }
  return body;
}

/** Run one archived storm end to end. Throws with the server's detail. */
export async function runReplay(eventId: string): Promise<ReplayTrajectory> {
  const response = await fetch(`/replay/${encodeURIComponent(eventId)}?step_hours=6`, {
    method: "POST",
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : `replay ${response.status}`;
    throw new Error(detail);
  }
  if (!body || typeof body !== "object" || !Array.isArray((body as { rows?: unknown }).rows)) {
    throw new Error("replay trajectory malformed");
  }
  const summary = (body as { summary?: unknown }).summary;
  if (!summary || typeof summary !== "object") {
    throw new Error("replay trajectory malformed");
  }
  return body as ReplayTrajectory;
}

export interface SessionTurn {
  turn_id: string;
  query: string;
  state: string;
  verdict: string | null;
  created_at: string;
  age_minutes: number;
  inheritable: boolean;
}

/** Real conversation history for this session, from the server. */
export async function forgetSession(sessionId: string): Promise<void> {
  const response = await fetch(`/session/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!response.ok) throw new Error(`session ${response.status}`);
}

export async function sessionHistory(sessionId: string): Promise<SessionTurn[]> {
  const response = await fetch(`/session/${encodeURIComponent(sessionId)}`);
  if (!response.ok) throw new Error(`session ${response.status}`);
  const body = await response.json();
  return Array.isArray(body.turns) ? body.turns : [];
}
