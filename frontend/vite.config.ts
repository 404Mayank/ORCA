import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs separately (uvicorn on 8000). Proxying in dev means the app
// calls same-origin paths and needs no base-URL config baked into the build.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/chat/stream": "http://127.0.0.1:8000",
      "/chat": "http://127.0.0.1:8000",
      "/readiness": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/session": "http://127.0.0.1:8000",
      "/settings": "http://127.0.0.1:8000",
      "/geo": "http://127.0.0.1:8000",
      "/chat/stream": "http://127.0.0.1:8000",
    },
  },
});
