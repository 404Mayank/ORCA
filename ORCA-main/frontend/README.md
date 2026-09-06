# ORCA frontend

React 18 · Vite · TypeScript · MapLibre GL JS.

```bash
npm install
npm run dev      # http://localhost:5173
```

Needs the API running: `uvicorn orchestrator.main:app` from the repo root.
Vite proxies `/chat`, `/readiness`, `/health` and `/session` to port 8000, so
no base URL is baked into the build.

## `src/types.ts` is generated — do not edit it

It comes from the Pydantic schemas in `core/schemas/`:

```bash
python scripts/gen_types.py
```

`tests/test_types.py` fails if the checked-in file is stale. CLAUDE.md forbids
hand-written frontend types because they drift within a week — and they earned
that rule here: the first draft of the evidence drawer guessed `Driver.observed`
and `ReasoningStep.step_id`, and the generated types caught both at compile
time.

## Layout

| File | Role |
|---|---|
| `App.tsx` | chat, status chips, session id, sample queries |
| `components/MapView.tsx` | basemap, origin marker, PFZ candidates, study box |
| `components/EvidenceDrawer.tsx` | the audit trail: drivers vs thresholds, claims, hypotheses, tool calls, reasoning trace |
| `api/client.ts` | `/chat` and `/readiness` |

## Notes

- **Nothing is positioned from a coordinate invented in the browser.** Markers
  come from `spatial_context.origin`; PFZ candidates are reconstructed from the
  `distance_km` / `bearing_deg` already in the claims.
- A refusal or clarification arrives as a normal 200 with a `state`. A thrown
  error means the API is unreachable — a different thing, shown differently.
- The map uses raster OSM tiles rather than a vector style, so there is no API
  key to expire mid-demo.
