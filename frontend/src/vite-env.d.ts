/// <reference types="vite/client" />

/**
 * Build-time configuration. Both are absent in dev: vite proxies to uvicorn
 * on the same origin (vite.config.ts) and the local API has no gate.
 */
interface ImportMetaEnv {
  /** Absolute API origin in a deployed build, e.g. the Modal URL. */
  readonly VITE_ORCA_API_URL?: string;
  /** Shared secret for the deployed API. Ships in the bundle; see api/client.ts. */
  readonly VITE_ORCA_API_KEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
