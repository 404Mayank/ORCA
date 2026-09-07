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
