import { defineConfig } from "@playwright/test";

// 端到端（S-04）：页面跑在 Vite 开发服务器上，/v1 请求由测试拦截返回假数据（不连真实 apid）。
// 浏览器用本机已装的 Edge（channel: msedge），不下载 Playwright 自带浏览器。
export default defineConfig({
  testDir: "tests",
  timeout: 30_000,
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5173",
    channel: process.env.PW_CHANNEL ?? "msedge",
    trace: "off",
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
