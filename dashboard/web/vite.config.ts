import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build to ../dist-relative? No — output stays inside web/ at web/dist, which the
// FastAPI app serves (and the Dockerfile copies). In dev, proxy the API to uvicorn.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
});
