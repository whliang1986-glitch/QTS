// UI-02 演示原型：DEMO 标记常驻、不请求任何接口、双视图切换（含键盘）、地图热点详情、减少动效、无障碍。
// 截图（SHOTS=1 时）写入 docs/design/screenshots/，作为 1920×1080 与 2560×1440 的验收证据。
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const SHOTS = process.env.SHOTS === "1";

test("DEMO 标记常驻，账户号脱敏，没有接口请求，没有违反 CSP", async ({ page }) => {
  // 记下全部请求：只允许同源的静态资源（页面、脚本、样式、Vite 开发服务器的模块），不允许 /v1 与外部地址
  const requests: string[] = [];
  page.on("request", (r) => requests.push(r.url()));
  const violations: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") violations.push(m.text()); });
  await page.goto("/demo.html");
  await page.getByTestId("tab-intel").click();
  await page.getByTestId("tab-decision").click();
  await expect(page.getByTestId("demo-banner")).toContainText("DEMO / SIMULATED");
  await expect(page.getByTestId("cx-mode")).toHaveText("DEMO · PAPER");
  expect(requests.filter((u) => u.includes("/v1"))).toEqual([]);
  expect(requests.filter((u) => !u.startsWith("http://127.0.0.1:5173/"))).toEqual([]);
  // Vite 开发服务器的热更新 websocket 被 connect-src 'none' 拦下属于预期，不算违规
  expect(violations.filter((v) => !v.includes("ws://") && !v.includes("[vite]"))).toEqual([]);
  await expect(page.getByTestId("panel-account")).toContainText("DU••••000");
  await expect(page.getByTestId("panel-strategy")).toContainText("N/A"); // 样本不足不填数字
});

test("双视图一键切换，外围面板不变，键盘可操作", async ({ page }) => {
  await page.goto("/demo.html");
  await expect(page.getByTestId("view-decision")).toBeVisible();
  await expect(page.getByTestId("view-decision")).toContainText("未启用"); // 首期没有 AI
  const account = await page.getByTestId("panel-account").textContent();
  await page.getByTestId("tab-intel").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("view-intel")).toBeVisible();
  await expect(page.getByTestId("tab-intel")).toHaveAttribute("aria-selected", "true");
  expect(await page.getByTestId("panel-account").textContent()).toBe(account);
  await page.getByTestId("tab-decision").click();
  await expect(page.getByTestId("view-decision")).toBeVisible();
});

test("地图热点可以打开事件详情，未定位事件不上地图，模型观点有标注", async ({ page }) => {
  await page.goto("/demo.html");
  await page.getByTestId("tab-intel").click();
  await page.getByRole("button", { name: /布鲁塞尔/ }).click();
  const detail = page.getByTestId("intel-detail");
  await expect(detail).toContainText("欧盟发布新一轮 AI 监管框架");
  await expect(detail).toContainText("模型观点");
  await expect(detail).toContainText("置信度 85%");
  await expect(page.getByText("未定位事件 1 条")).toBeVisible();
  await expect(page.locator(".hot")).toHaveCount(5);
});

test("减少动效开关关闭全部动画", async ({ page }) => {
  await page.goto("/demo.html");
  await page.getByTestId("motion-toggle").click();
  await expect(page.getByTestId("console")).toHaveClass(/cx-still/);
  const animation = await page.locator(".cx-ring-1").evaluate((el) => getComputedStyle(el).animationName);
  expect(animation).toBe("none");
});

test("没有严重的无障碍问题", async ({ page }) => {
  await page.goto("/demo.html");
  for (const tab of ["tab-decision", "tab-intel"]) {
    await page.getByTestId(tab).click();
    await page.waitForTimeout(400); // 等切换的淡入（260 ms）结束，避免把过渡中的半透明当成对比度不足
    const result = await new AxeBuilder({ page }).include("[data-testid=console]").analyze();
    const serious = result.violations.filter((v) => v.impact === "critical" || v.impact === "serious");
    expect(serious.map((v) => `${v.id}: ${v.help} @ ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  }
});

for (const [width, height] of [[1920, 1080], [2560, 1440]] as const) {
  test(`${width}×${height} 下没有横向溢出`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto("/demo.html");
    for (const tab of ["tab-decision", "tab-intel"]) {
      await page.getByTestId(tab).click();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(overflow).toBe(false);
      if (SHOTS) {
        await page.screenshot({ path: `../docs/design/screenshots/console-demo-${tab.slice(4)}-${width}x${height}.png` });
      }
    }
  });
}
