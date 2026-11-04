import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时把 /v1 转发到本机 apid（S-02/S-03）；生产由 apid 或 Tauri 壳托管静态文件（S-06）。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/v1": { target: "http://127.0.0.1:8730", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
