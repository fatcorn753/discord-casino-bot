import { defineConfig } from "vite";

// 開発中は Bot の Activity のサーバー（127.0.0.1:8787）に、API と WebSocket を中継する
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/ws": { target: "ws://127.0.0.1:8787", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
});
