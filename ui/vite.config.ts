import { defineConfig } from "vite";
import solid from "vite-plugin-solid";

// Dev: `npm run dev` serves on :5173 and proxies /api to the FastAPI service.
// Prod: `npm run build` emits ./dist, which the FastAPI app serves at "/".
export default defineConfig({
  plugins: [solid()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
