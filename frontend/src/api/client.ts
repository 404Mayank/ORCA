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
