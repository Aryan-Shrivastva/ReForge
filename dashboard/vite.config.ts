import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

declare const process: { env: Record<string, string | undefined> };

// Dev-server proxy target; override with REFORGE_API_URL when the API is not on :8000.
const api = process.env.REFORGE_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/runs": api,
      "/benchmarks": api,
      "/health": api
    }
  }
});
