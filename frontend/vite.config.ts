import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built UI is served by the JARVIS daemon under /ui/ (index.html at /).
export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/jarvis/ui",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 2500,
  },
  server: {
    // `npm run dev` against a running daemon: open http://localhost:5173/ui/#token=...
    proxy: { "/v1": { target: "http://127.0.0.1:8765", ws: true } },
  },
});
