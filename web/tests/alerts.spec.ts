// S-05a 端到端：告警页与审计日志页。读不到是“无法获取”而不是“没有告警”；确认只有 owner，路径里只有编号。
import { expect, test, type Page, type Route } from "@playwright/test";

type Json = Record<string, unknown>;

const PAPER: Json = {
  mode: "PAPER",
  halt: { halted: false, reason: null, since: null, set_by: null },
  changed_at: "2026-11-04T14:00:00.000000+00:00",
  changed_by: "dylan",
};

function alert(id: number, level: string, extra: Json = {}): Json {
  return {
    alert_id: id, key: `k${id}`, level, code: "tradingd_heartbeat_lost",
    text: { zh: `[${level}] 中文告警 ${id}`, en: `[${level}] English alert ${id}` },
    raised_at: "2026-11-04T14:00:00.000000+00:00", acked_by: null, acked_at: null, resolved_at: null,
    ...extra,
  };
}

interface Mock {
  role: "owner" | "viewer";
  alerts: () => { code: number; body: Json };
  audit: () => { code: number; body: Json };
  acks: { path: string; body: string | null }[];
  ackReply: () => { code: number; body: Json };
  auditQueries: string[];
}

function json(route: Route, code: number, body: Json): Promise<void> {
  return route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockApi(page: Page, overrides: Partial<Mock> = {}): Promise<Mock> {
  const mock: Mock = {
    role: "owner",
    alerts: () => ({ code: 200, body: { alerts: [alert(3, "P0"), alert(2, "P1", { acked_by: "dylan" }), alert(1, "P1", { resolved_at: "2026-11-04T14:01:00+00:00" })] } }),
    audit: () => ({ code: 200, body: { entries: [{ seq: 9, at: "2026-11-04T14:02:00+00:00", action: "alert_ack_requested", actor: "dylan", detail: { alert_id: "3" } }] } }),
    acks: [],
    ackReply: () => ({ code: 200, body: { alert_id: 3, requested: true, already: false } }),
    auditQueries: [],
    ...overrides,
  };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.headers().authorization !== "Bearer test-token") {
      return json(route, 401, { error: "令牌无效", retry_same_request_id: false });
    }
    if (url.pathname === "/v1/whoami") return json(route, 200, { name: "dylan", role: mock.role });
    if (url.pathname === "/v1/status") return json(route, 200, PAPER);
    if (url.pathname === "/v1/alerts") {
      const { code, body } = mock.alerts();
      return json(route, code, body);
    }
    if (/^\/v1\/alerts\/\d+\/ack$/.test(url.pathname)) {
      mock.acks.push({ path: url.pathname, body: request.postData() });
      const { code, body } = mock.ackReply();
      return json(route, code, body);
    }
    if (url.pathname === "/v1/audit") {
      mock.auditQueries.push(url.search);
      const { code, body } = mock.audit();
      return json(route, code, body);
    }
    return json(route, 404, { error: "没有这个接口", retry_same_request_id: false });
  });
  return mock;
}

async function open(page: Page, tab: "alerts" | "audit"): Promise<void> {
  await page.goto("/");
  await page.getByTestId("token-input").fill("test-token");
  await page.getByTestId("login").click();
  await expect(page.getByTestId("mode-badge")).toBeVisible();
  await page.getByTestId(`nav-${tab}`).click();
}

test("告警列表默认只看未解除，可切换看全部", async ({ page }) => {
  await mockApi(page);
  await open(page, "alerts");
  await expect(page.getByTestId("alert-3")).toContainText("中文告警 3");
  await expect(page.getByTestId("alert-2")).toContainText("已确认（dylan）");
  await expect(page.getByTestId("alert-1")).toHaveCount(0);
  await page.getByTestId("alerts-only-open").uncheck();
  await expect(page.getByTestId("alert-1")).toContainText("已解除");
});

test("读不到告警显示无法获取，不显示没有告警", async ({ page }) => {
  await mockApi(page, { alerts: () => ({ code: 503, body: { error: "无法获取告警", retry_same_request_id: false } }) });
  await open(page, "alerts");
  await expect(page.getByTestId("alerts-unavailable")).toBeVisible();
  await expect(page.getByTestId("alerts-empty")).toHaveCount(0);
  await expect(page.getByTestId("alerts-table")).toHaveCount(0);
});

test("没有未解除告警时显示空状态", async ({ page }) => {
  await mockApi(page, { alerts: () => ({ code: 200, body: { alerts: [] } }) });
  await open(page, "alerts");
  await expect(page.getByTestId("alerts-empty")).toContainText("当前没有未解除的告警");
});

test("owner 确认告警：只发编号，不带操作者", async ({ page }) => {
  const mock = await mockApi(page);
  await open(page, "alerts");
  await expect(page.getByTestId("ack-2")).toHaveCount(0); // 已确认的没有按钮
  await page.getByTestId("ack-3").click();
  await expect(page.getByTestId("alert-feedback-3")).toContainText("已提交确认");
  expect(mock.acks).toEqual([{ path: "/v1/alerts/3/ack", body: null }]);
});

test("确认失败如实提示", async ({ page }) => {
  await mockApi(page, { ackReply: () => ({ code: 503, body: { error: "确认未记录：api.db 写入失败，请重试", retry_same_request_id: false } }) });
  await open(page, "alerts");
  await page.getByTestId("ack-3").click();
  await expect(page.getByTestId("alert-feedback-3")).toContainText("确认没有记录");
});

test("viewer 看得到告警但没有确认按钮", async ({ page }) => {
  await mockApi(page, { role: "viewer" });
  await open(page, "alerts");
  await expect(page.getByTestId("alert-3")).toBeVisible();
  await expect(page.getByTestId("ack-3")).toHaveCount(0);
  await expect(page.getByTestId("alerts-viewer")).toBeVisible();
});

test("英文界面显示英文告警", async ({ page }) => {
  await mockApi(page);
  await page.addInitScript(() => localStorage.setItem("qts.prefs", JSON.stringify({ lang: "en" })));
  await open(page, "alerts");
  await expect(page.getByTestId("alert-3")).toContainText("English alert 3");
});

test("审计日志：列出记录与详情", async ({ page }) => {
  await mockApi(page);
  await open(page, "audit");
  await expect(page.getByTestId("audit-table")).toContainText("alert_ack_requested");
  await expect(page.getByTestId("audit-table")).toContainText("alert_id=3");
});

test("审计日志 403 提示只有 owner 可看", async ({ page }) => {
  await mockApi(page, { audit: () => ({ code: 403, body: { error: "只有 owner 能发命令", retry_same_request_id: false } }) });
  await open(page, "audit");
  await expect(page.getByTestId("audit-unavailable")).toContainText("只有 owner 令牌能查看审计记录");
  await expect(page.getByTestId("audit-empty")).toHaveCount(0);
});

test("审计日志满 100 条时可以加载更早的记录", async ({ page }) => {
  const entries = Array.from({ length: 100 }, (_, i) => ({ seq: 200 - i, at: "2026-11-04T14:00:00+00:00", action: "a", actor: "dylan", detail: {} }));
  const mock = await mockApi(page, { audit: () => ({ code: 200, body: { entries } }) });
  await open(page, "audit");
  await page.getByTestId("audit-more").click();
  await expect.poll(() => mock.auditQueries.at(-1)).toContain("before=101");
});
