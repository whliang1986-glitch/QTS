// S-13 设计把关：截图（SHOTS=1 时写入 docs/design/screenshots/）、视觉回归基线、无障碍检查、键盘操作。
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

type Json = Record<string, unknown>;
type Theme = "dark" | "light";

const STATUS: Json = {
  mode: "PAPER",
  halt: { halted: false, reason: null, since: null, set_by: null },
  changed_at: "2026-11-03T14:00:00.000000+00:00",
  changed_by: "dylan",
};
const ORDERS: Json = {
  orders: [
    ["spy-rebal-1103", "SPY", "BUY", "12", "512.40", "OPEN", "FILLED"],
    ["ief-rebal-1103", "IEF", "SELL", "30", "94.15", "REDUCE", "SUBMITTED"],
    ["qqq-stop-1101", "QQQ", "SELL", "8", null, "PROTECTIVE_STOP", "SUBMITTED"],
    ["tlt-rebal-1031", "TLT", "SELL", "5", "88.02", "REDUCE", "CANCELLED"],
    ["gld-rebal-1031", "GLD", "BUY", "3", "241.10", "OPEN", "UNKNOWN"],
  ].map(([id, symbol, side, qty, limit, cls, status]) => ({
    intent_id: id, created_at: "2026-11-03T14:30:00+00:00", strategy_id: "etf-rotation",
    strategy_version: "1", symbol, side, quantity: qty, order_type: limit ? "LIMIT" : "STOP",
    limit_price: limit, stop_price: limit ? null : "401.00", order_class: cls, time_in_force: "DAY",
    expires_at: null, status, updated_at: "2026-11-03T14:31:07.123456+00:00",
  })),
};

function json(route: Route, body: Json): Promise<void> {
  return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
}

async function prepare(page: Page, theme: Theme, loggedIn = true): Promise<void> {
  await page.addInitScript(([t, li]) => {
    localStorage.setItem("qts.prefs", JSON.stringify({ theme: t }));
    if (li) sessionStorage.setItem("qts.token", "test-token");
  }, [theme, loggedIn] as const);
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/whoami") return json(route, { name: "dylan", role: "owner" });
    if (path === "/v1/status") return json(route, STATUS);
    if (path === "/v1/orders") return json(route, ORDERS);
    return route.fulfill({ status: 404, body: "{}" });
  });
}

const PAGES = ["showroom", "login", "overview", "orders", "positions", "risk", "emergency"] as const;
type PageName = (typeof PAGES)[number];

async function open(page: Page, name: PageName, theme: Theme): Promise<void> {
  await prepare(page, theme, name !== "login");
  await page.goto(name === "showroom" ? "/#showroom" : "/");
  if (name === "showroom") {
    await expect(page.getByTestId("showroom")).toBeVisible();
  } else if (name === "login") {
    await expect(page.getByTestId("token-input")).toBeVisible();
  } else {
    await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
    if (name !== "overview") await page.getByTestId(`nav-${name}`).click();
    if (name === "orders") await expect(page.getByTestId("orders-table")).toBeVisible();
    if (name === "emergency") await expect(page.getByTestId("emergency-controls")).toBeVisible();
  }
  await page.evaluate(() => document.fonts.ready);
}

const WIDTHS = { desktop: [1440, 900], tablet: [834, 1112], mobile: [390, 844] } as const;

test.describe("截图（SHOTS=1）", () => {
  test.skip(!process.env.SHOTS, "只在需要更新设计截图时运行");
  for (const theme of ["dark", "light"] as const) {
    for (const [size, [width, height]] of Object.entries(WIDTHS)) {
      for (const name of PAGES) {
        test(`${name}-${theme}-${size}`, async ({ page }) => {
          await page.setViewportSize({ width, height });
          await open(page, name, theme);
          await page.screenshot({
            path: `../docs/design/screenshots/${name}-${theme}-${size}.png`,
            fullPage: true,
            animations: "disabled",
          });
        });
      }
    }
  }
});

test.describe("视觉回归", () => {
  for (const [name, theme, size] of [
    ["showroom", "dark", "desktop"], ["showroom", "light", "desktop"],
    ["overview", "dark", "desktop"], ["emergency", "light", "mobile"],
  ] as const) {
    test(`${name}-${theme}-${size}`, async ({ page }) => {
      const [width, height] = WIDTHS[size];
      await page.setViewportSize({ width, height });
      await open(page, name, theme);
      await expect(page).toHaveScreenshot(`${name}-${theme}-${size}.png`, {
        fullPage: true, animations: "disabled", maxDiffPixelRatio: 0.01,
      });
    });
  }
});

test.describe("无障碍", () => {
  for (const theme of ["dark", "light"] as const) {
    for (const name of PAGES) {
      test(`axe ${name}-${theme}`, async ({ page }) => {
        await page.setViewportSize({ width: 1440, height: 900 });
        await open(page, name, theme);
        const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
        const serious = result.violations
          .filter((v) => v.impact === "serious" || v.impact === "critical")
          .map((v) => `${v.id}: ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
        expect(serious).toEqual([]);
      });
    }
  }

  async function tabTo(page: Page, testId: string, max = 40): Promise<void> {
    for (let i = 0; i < max; i++) {
      await page.keyboard.press("Tab");
      const focused = await page.evaluate(() => document.activeElement?.getAttribute("data-testid"));
      if (focused === testId) return;
    }
    throw new Error(`按 Tab ${max} 次也到不了 ${testId}`);
  }

  test("键盘：登录、切页、展开急停并取消（只用 Tab 与 Enter）", async ({ page }) => {
    await prepare(page, "dark", false);
    await page.goto("/");
    await expect(page.getByTestId("token-input")).toBeFocused();
    await page.keyboard.type("test-token");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
    await tabTo(page, "nav-emergency");
    await page.keyboard.press("Enter");
    await tabTo(page, "open-cancel-open");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("confirm-cancel-open")).toBeVisible();
    await tabTo(page, "cancel-cancel-open");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("open-cancel-open")).toBeVisible();
  });

  test("焦点清晰可见", async ({ page }) => {
    await open(page, "overview", "light");
    await page.getByTestId("nav-orders").focus();
    const outline = await page.getByTestId("nav-orders").evaluate((el) => getComputedStyle(el).outlineStyle);
    expect(outline).not.toBe("none");
  });
});

test.describe("无障碍：风险状态", () => {
  const states: [string, Json][] = [
    ["HALT", { ...STATUS, halt: { halted: true, reason: "人工急停", since: "2026-11-03T14:05:00+00:00", set_by: "dylan" } }],
    ["未知", { ...STATUS, halt: null }],
  ];
  for (const theme of ["dark", "light"] as const) {
    for (const [label, status] of states) {
      test(`axe ${label}-${theme}`, async ({ page }) => {
        await prepare(page, theme);
        await page.route("**/v1/status", (route) => json(route, status));
        await page.goto("/");
        await expect(page.getByTestId("mode-badge")).toHaveText(label);
        await page.getByTestId("nav-emergency").click();
        await page.getByTestId("open-cancel-stops").click(); // 展开后的高危表单也要扫
        const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
        const serious = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical")
          .map((v) => `${v.id}: ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
        expect(serious).toEqual([]);
      });
    }
  }
});
