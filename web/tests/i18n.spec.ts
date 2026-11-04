// S-12 多语言：语言包完整、默认跟随浏览器并回退中文、切换后记住、风险文案两种语言都在、确认短语不翻译。
import { expect, test, type Page, type Route } from "@playwright/test";
import { MESSAGES, detectLang, translate } from "../src/i18n";

type Json = Record<string, unknown>;
const PAPER: Json = {
  mode: "PAPER", halt: { halted: false, reason: null, since: null, set_by: null },
  changed_at: "2026-11-04T14:00:00+00:00", changed_by: "dylan",
};

function json(route: Route, body: Json, status = 200): Promise<void> {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function mock(page: Page, status: Json = PAPER): Promise<void> {
  await page.addInitScript(() => sessionStorage.setItem("qts.token", "test-token"));
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/v1/whoami") return json(route, { name: "dylan", role: "owner" });
    if (path === "/v1/status") return json(route, status);
    if (path === "/v1/orders") return json(route, { orders: [] });
    if (path === "/v1/commands/confirmations") {
      return json(route, { confirmation_id: "c-1", name: "emergency_flatten", expires_at: "2026-11-04T14:01:00+00:00" });
    }
    if (path === "/v1/commands") {
      return json(route, { request_id: "x", ok: true, replayed: false, detail: {
        preview_id: "p-1", confirmation: "平仓 p-1", warnings: [],
        lines: [{ symbol: "SPY", quantity: "10", limit_price: "99.50", max_slippage: "0.50" }],
      } });
    }
    return json(route, {}, 404);
  });
}

test("语言包：中英文的键完全一致", () => {
  expect(Object.keys(MESSAGES.en).sort()).toEqual(Object.keys(MESSAGES.zh).sort());
});

test("语言包：每条文案的占位符两种语言一致，且没有空文案", () => {
  const holes = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
  for (const key of Object.keys(MESSAGES.zh) as (keyof typeof MESSAGES.zh)[]) {
    expect(holes(MESSAGES.en[key]), key).toEqual(holes(MESSAGES.zh[key]));
    expect(MESSAGES.en[key].trim(), key).not.toBe("");
    expect(MESSAGES.zh[key].trim(), key).not.toBe("");
  }
});

test("英文语言包里没有混进中文（服务端原文与确认短语除外）", () => {
  for (const [key, text] of Object.entries(MESSAGES.en)) {
    expect(/[\u4e00-\u9fff]/.test(text), key).toBe(false);
  }
});

test("默认语言：跟随浏览器，认不出时用中文", () => {
  expect(detectLang(["en-US", "zh-CN"])).toBe("en");
  expect(detectLang(["zh-TW"])).toBe("zh");
  expect(detectLang(["fr-FR", "de"])).toBe("zh");
  expect(detectLang([])).toBe("zh");
  expect(detectLang(["fr-FR", "en-GB"])).toBe("en");
});

test("参数替换", () => {
  expect(translate("en", "cmd.retrySame", { id: "web-1" })).toBe("Retry with the same ID (web-1)");
  expect(translate("zh", "banner.halt", { reason: "人工急停" })).toContain("人工急停");
});

test.describe("英文浏览器", () => {
  test.use({ locale: "en-US" });

  test("默认显示英文，模式 PAPER 不翻译", async ({ page }) => {
    await mock(page);
    await page.goto("/");
    await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
    await expect(page.getByTestId("nav-overview")).toHaveText("Overview");
    await expect(page.getByTestId("connection")).toHaveText("Connected");
    expect(await page.evaluate(() => document.documentElement.lang)).toBe("en");
  });

  test("状态不可信时显示 UNKNOWN 并给出英文横幅", async ({ page }) => {
    await mock(page, { ...PAPER, halt: null });
    await page.goto("/");
    await expect(page.getByTestId("mode-badge")).toHaveText("UNKNOWN");
    await expect(page.getByTestId("untrusted-banner")).toContainText("shown as UNKNOWN");
  });

  test("急停说明是英文，但服务端给的确认短语原样显示", async ({ page }) => {
    await mock(page);
    await page.goto("/");
    await page.getByTestId("nav-emergency").click();
    await expect(page.getByTestId("control-cancel-open")).toContainText("Protective stops are not affected");
    await page.getByTestId("open-flatten").click();
    await page.getByTestId("flatten-preview").click();
    await expect(page.getByTestId("flatten-expected")).toHaveText("平仓 p-1");
    // 提交的也是服务端给的中文原文：英文界面不会翻译或改写确认短语
    const sent: Record<string, unknown>[] = [];
    page.on("request", (r) => {
      if (r.url().endsWith("/v1/commands") && r.method() === "POST") sent.push(r.postDataJSON());
    });
    await page.getByTestId("flatten-phrase").fill("平仓 p-1");
    await page.getByTestId("confirm-flatten").click();
    await expect.poll(() => sent.length).toBeGreaterThan(0);
    expect(sent[0]).toMatchObject({ name: "emergency_flatten", params: { preview_id: "p-1", confirmation: "平仓 p-1" } });
  });

  test("切换到中文后记住，刷新仍是中文", async ({ page }) => {
    await mock(page);
    await page.goto("/");
    await page.getByTestId("lang-zh").click();
    await expect(page.getByTestId("nav-overview")).toHaveText("总览");
    await page.reload();
    await expect(page.getByTestId("nav-overview")).toHaveText("总览");
    const prefs = await page.evaluate(() => localStorage.getItem("qts.prefs"));
    expect(JSON.parse(prefs ?? "{}").lang).toBe("zh");
    expect(JSON.parse(prefs ?? "{}").theme).toBe("dark"); // 切换语言不覆盖主题偏好
  });
});

test.describe("不认识的浏览器语言", () => {
  test.use({ locale: "fr-FR" });

  test("回退到中文", async ({ page }) => {
    await mock(page);
    await page.goto("/");
    await expect(page.getByTestId("nav-overview")).toHaveText("总览");
    await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
  });
});

test("申请二次确认失败：显示“没有发出”，不给同号重试，也不发出命令", async ({ page }) => {
  await mock(page);
  const commands: string[] = [];
  await page.route("**/v1/commands/confirmations", (route) =>
    json(route, { error: "未使用的确认太多，请稍后再试", retry_same_request_id: false }, 429));
  page.on("request", (r) => { if (r.url().endsWith("/v1/commands") && r.method() === "POST") commands.push(r.url()); });
  await page.goto("/");
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-stops").click();
  await page.getByTestId("stops-phrase").fill("确认撤销全部保护性止损");
  await page.getByTestId("confirm-cancel-stops").click();
  await expect(page.getByTestId("command-error")).toContainText("命令没有发出");
  await expect(page.getByTestId("retry-same")).toHaveCount(0);
  await expect(page.getByTestId("cancel-cancel-stops")).toBeVisible();
  expect(commands).toEqual([]);
});
