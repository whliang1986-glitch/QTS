// S-04 端到端：模式标识、断连提示、急停四控件（含高危两道确认）、同号重试、viewer 只读。
import { expect, test, type Page, type Route } from "@playwright/test";

type Json = Record<string, unknown>;

const PAPER: Json = {
  mode: "PAPER",
  halt: { halted: false, reason: null, since: null, set_by: null },
  changed_at: "2026-11-03T14:00:00.000000+00:00",
  changed_by: "dylan",
};
const HALTED: Json = {
  ...PAPER,
  halt: { halted: true, reason: "人工急停", since: "2026-11-03T14:05:00+00:00", set_by: "dylan" },
};

interface Mock {
  status: () => { code: number; body: Json };
  role: "owner" | "viewer";
  commands: Json[];
  confirmations: Json[];
  commandReply: (body: Json, n: number) => { code: number; body: Json };
}

function json(route: Route, code: number, body: Json): Promise<void> {
  return route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockApi(page: Page, overrides: Partial<Mock> = {}): Promise<Mock> {
  const mock: Mock = {
    status: () => ({ code: 200, body: PAPER }),
    role: "owner",
    commands: [],
    confirmations: [],
    commandReply: () => ({ code: 200, body: { request_id: "x", ok: true, replayed: false, detail: {} } }),
    ...overrides,
  };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.headers().authorization !== "Bearer test-token") {
      return json(route, 401, { error: "令牌无效", retry_same_request_id: false });
    }
    if (url.pathname === "/v1/whoami") return json(route, 200, { name: "dylan", role: mock.role });
    if (url.pathname === "/v1/status") {
      const { code, body } = mock.status();
      return json(route, code, body);
    }
    if (url.pathname === "/v1/orders") return json(route, 200, { orders: [] });
    if (url.pathname === "/v1/commands/confirmations") {
      const body = request.postDataJSON() as Json;
      mock.confirmations.push(body);
      return json(route, 200, {
        confirmation_id: `c-${mock.confirmations.length}`, name: body.name,
        expires_at: "2026-11-03T14:01:00+00:00",
      });
    }
    if (url.pathname === "/v1/commands") {
      const body = request.postDataJSON() as Json;
      mock.commands.push(body);
      const { code, body: reply } = mock.commandReply(body, mock.commands.length);
      return json(route, code, reply);
    }
    return json(route, 404, { error: "没有这个接口", retry_same_request_id: false });
  });
  return mock;
}

async function login(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByTestId("token-input").fill("test-token");
  await page.getByTestId("login").click();
  await expect(page.getByTestId("mode-badge")).toBeVisible();
}

test("PAPER 模式标识", async ({ page }) => {
  await mockApi(page);
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
  await expect(page.getByTestId("stale-banner")).toHaveCount(0);
});

test("HALT 时模式标识显示 HALT", async ({ page }) => {
  await mockApi(page, { status: () => ({ code: 200, body: HALTED }) });
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("HALT");
  await expect(page.getByTestId("overview-halt")).toContainText("人工急停");
});

test("账本不可读时显示未知并提示数据过期", async ({ page }) => {
  let healthy = true;
  await mockApi(page, {
    status: () => (healthy
      ? { code: 200, body: PAPER }
      : { code: 503, body: { error: "账本不可读", retry_same_request_id: false } }),
  });
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
  healthy = false;
  await expect(page.getByTestId("mode-badge")).toHaveText("未知", { timeout: 10_000 });
  await expect(page.getByTestId("stale-banner")).toContainText("服务器仍在运行");
});

test("断网时显示断开提示，不显示旧模式", async ({ page }) => {
  await mockApi(page);
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
  await page.unroute("**/v1/**");
  await page.route("**/v1/**", (route) => route.abort("connectionrefused"));
  await expect(page.getByTestId("connection")).toHaveText("连接断开", { timeout: 10_000 });
  await expect(page.getByTestId("mode-badge")).toHaveText("未知");
  await expect(page.getByTestId("stale-banner")).toBeVisible();
});

test("急停四个控件各自独立", async ({ page }) => {
  const mock = await mockApi(page);
  await login(page);
  await page.getByTestId("nav-emergency").click();
  for (const id of ["stop", "cancel-open", "cancel-stops", "flatten"]) {
    await expect(page.getByTestId(`control-${id}`)).toBeVisible();
    await page.getByTestId(`open-${id}`).click(); // 展开不会发任何命令
  }
  expect(mock.commands).toHaveLength(0);
  expect(mock.confirmations).toHaveLength(0);
});

test("停止新增必须填写原因，确认后才发送", async ({ page }) => {
  const mock = await mockApi(page);
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-stop").click();
  await expect(page.getByTestId("confirm-stop")).toBeDisabled();
  await page.getByTestId("stop-reason").fill("盘中异常");
  await page.getByTestId("confirm-stop").click();
  await expect(page.getByTestId("command-result")).toContainText("已执行");
  expect(mock.commands).toHaveLength(1);
  expect(mock.commands[0]).toMatchObject({ name: "stop_new_orders", params: { reason: "盘中异常" } });
  expect(mock.commands[0]).not.toHaveProperty("confirmation_id");
});

test("撤保护性止损：先申请二次确认，再带确认短语提交", async ({ page }) => {
  const mock = await mockApi(page);
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-stops").click();
  await expect(page.getByTestId("confirm-cancel-stops")).toBeDisabled();
  await page.getByTestId("stops-phrase").fill("确认撤销全部保护性止损");
  await page.getByTestId("confirm-cancel-stops").click();
  await expect(page.getByTestId("command-result")).toBeVisible();
  expect(mock.confirmations).toEqual([
    { name: "cancel_protective_stops", params: { confirmation: "确认撤销全部保护性止损" } },
  ]);
  expect(mock.commands[0]).toMatchObject({
    name: "cancel_protective_stops",
    params: { confirmation: "确认撤销全部保护性止损" },
    confirmation_id: "c-1",
  });
});

test("紧急平仓：预览 → 二次确认 → 输入预览给出的短语", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (body) => body.name === "preview_flatten"
      ? { code: 200, body: { request_id: "x", ok: true, replayed: false, detail: {
          preview_id: "p-1", confirmation: "平仓 p-1",
          lines: [{ symbol: "SPY", quantity: "10", limit_price: "99.50", max_slippage: "0.50" }],
          warnings: [] } } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: false, detail: { lines: [] } } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-flatten").click();
  await page.getByTestId("flatten-preview").click();
  await expect(page.getByTestId("flatten-lines")).toContainText("SPY");
  await expect(page.getByTestId("flatten-expected")).toHaveText("平仓 p-1");
  await expect(page.getByTestId("confirm-flatten")).toBeDisabled();
  await page.getByTestId("flatten-phrase").fill("平仓 p-1");
  await page.getByTestId("confirm-flatten").click();
  await expect(page.getByTestId("command-result")).toContainText("已执行");
  expect(mock.commands.map((c) => c.name)).toEqual(["preview_flatten", "emergency_flatten"]);
  expect(mock.commands[1]).toMatchObject({
    params: { preview_id: "p-1", confirmation: "平仓 p-1" }, confirmation_id: "c-1",
  });
});

test("结果未知（504）后只能用同一编号重试", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_body, n) => n === 1
      ? { code: 504, body: { error: "命令已发出但结果未知", retry_same_request_id: true } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: true, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("command-error")).toContainText("结果未知");
  await expect(page.getByTestId("confirm-cancel-open")).toBeDisabled(); // 不能换号重发
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toContainText("之前那次的结果");
  expect(mock.commands).toHaveLength(2);
  expect(mock.commands[0]?.request_id).toBe(mock.commands[1]?.request_id);
});

test("viewer 看不到急停控件", async ({ page }) => {
  await mockApi(page, { role: "viewer" });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await expect(page.getByTestId("viewer-note")).toBeVisible();
  await expect(page.getByTestId("emergency-controls")).toHaveCount(0);
});

test("令牌无效时停在登录页", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.getByTestId("token-input").fill("wrong");
  await page.getByTestId("login").click();
  await expect(page.getByTestId("login-error")).toContainText("令牌无效");
});

for (const [label, body] of [
  ["模式为空", { ...PAPER, mode: null }],
  ["HALT 状态未知", { ...PAPER, halt: null }],
  ["不认识的模式", { ...PAPER, mode: "ASSISTED_LIVE" }],
] as const) {
  test(`模式标识只有三种：${label}时显示未知`, async ({ page }) => {
    await mockApi(page, { status: () => ({ code: 200, body: body as Json }) });
    await login(page);
    await expect(page.getByTestId("mode-badge")).toHaveText("未知");
  });
}

test("请求挂起不返回时，10 秒后降为未知", async ({ page }) => {
  let hang = false;
  await mockApi(page);
  await page.route("**/v1/status", async (route) => {
    if (hang) return; // 永不回答
    return json(route, 200, PAPER);
  });
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("PAPER");
  hang = true;
  await expect(page.getByTestId("mode-badge")).toHaveText("未知", { timeout: 15_000 });
  await expect(page.getByTestId("stale-banner")).toBeVisible();
});

test("高危命令 504 后确认号过期：重新申请确认号，编号不变", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_b, n) => n === 1
      ? { code: 504, body: { error: "结果未知", retry_same_request_id: true } }
      : n === 2
        ? { code: 403, body: { error: "二次确认不存在或已过期", retry_same_request_id: false } }
        : { code: 200, body: { request_id: "x", ok: true, replayed: true, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-stops").click();
  await page.getByTestId("stops-phrase").fill("确认撤销全部保护性止损");
  await page.getByTestId("confirm-cancel-stops").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  await page.getByTestId("retry-same").click(); // 403：确认号过期
  await expect(page.getByTestId("retry-same")).toBeVisible(); // 仍然只能同号重试
  await expect(page.getByTestId("cancel-cancel-stops")).toHaveCount(0);
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toContainText("之前那次的结果");
  const ids = mock.commands.map((c) => c.request_id);
  expect(new Set(ids).size).toBe(1);
  expect(mock.commands.map((c) => c.confirmation_id)).toEqual(["c-1", "c-1", "c-2"]);
});

test("结果未知的操作在切页后仍保留，不能换号", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_b, n) => n === 1
      ? { code: 504, body: { error: "结果未知", retry_same_request_id: true } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: true, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  await page.getByTestId("nav-overview").click();
  await page.getByTestId("nav-emergency").click();
  await expect(page.getByTestId("retry-same")).toBeVisible(); // 自动展开，编号还在
  await expect(page.getByTestId("confirm-cancel-open")).toBeDisabled();
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toBeVisible();
  expect(mock.commands[0]?.request_id).toBe(mock.commands[1]?.request_id);
});

test("命令请求断网：按结果未知处理，只能同号重试", async ({ page }) => {
  const mock = await mockApi(page);
  let drop = true;
  await page.route("**/v1/commands", async (route) => {
    mock.commands.push(route.request().postDataJSON() as Json);
    if (drop) return route.abort("connectionreset");
    return json(route, 200, { request_id: "x", ok: true, replayed: true, detail: {} });
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  drop = false;
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toBeVisible();
  expect(mock.commands[0]?.request_id).toBe(mock.commands[1]?.request_id);
});

test("平仓预览 504 后不能换号再生成", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: () => ({ code: 504, body: { error: "结果未知", retry_same_request_id: true } }),
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-flatten").click();
  await page.getByTestId("flatten-preview").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  await expect(page.getByTestId("flatten-preview")).toBeDisabled();
  expect(mock.commands).toHaveLength(1);
});

test("显式放弃后才能开始新的操作", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_b, n) => n === 1
      ? { code: 504, body: { error: "结果未知", retry_same_request_id: true } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: false, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("confirm-cancel-open")).toBeDisabled();
  await page.getByTestId("abandon").click();
  await page.getByTestId("confirm-cancel-open").click(); // 放弃后是一次新的操作
  await expect(page.getByTestId("command-result")).toContainText("已执行");
  expect(mock.commands[0]?.request_id).not.toBe(mock.commands[1]?.request_id);
});

test("令牌中途失效：回到登录页并清除令牌，不进 localStorage", async ({ page }) => {
  let valid = true;
  await mockApi(page);
  await page.route("**/v1/status", (route) => (valid
    ? json(route, 200, PAPER)
    : json(route, 401, { error: "令牌无效", retry_same_request_id: false })));
  await login(page);
  // localStorage 只放界面偏好（S-13），不放令牌
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain("test-token");
  valid = false;
  await expect(page.getByTestId("token-input")).toBeVisible({ timeout: 10_000 });
  expect(await page.evaluate(() => sessionStorage.getItem("qts.token"))).toBeNull();
});

test("令牌失效重新登录后，结果未知的编号仍在", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_b, n) => n === 1
      ? { code: 504, body: { error: "结果未知", retry_same_request_id: true } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: true, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await page.getByTestId("token-input").fill("test-token");
  await page.getByTestId("login").click();
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toBeVisible();
  expect(mock.commands[0]?.request_id).toBe(mock.commands[1]?.request_id);
});

test("撤销开仓单的说明写明会撤减仓单（与服务端一致）", async ({ page }) => {
  await mockApi(page);
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await expect(page.getByTestId("control-cancel-open")).toContainText("开仓单与减仓单");
  await expect(page.getByTestId("control-cancel-open")).toContainText("保护性止损不受影响");
  await expect(page.getByTestId("control-flatten")).toContainText("须先撤止损");
});

test("打开陈列室再回来，结果未知的编号仍在", async ({ page }) => {
  const mock = await mockApi(page, {
    commandReply: (_b, n) => n === 1
      ? { code: 504, body: { error: "结果未知", retry_same_request_id: true } }
      : { code: 200, body: { request_id: "x", ok: true, replayed: true, detail: {} } },
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("retry-same")).toBeVisible();
  await page.evaluate(() => { window.location.hash = "#showroom"; });
  await expect(page.getByTestId("showroom")).toBeVisible();
  await page.evaluate(() => { window.location.hash = ""; });
  await page.getByTestId("nav-emergency").click();
  await expect(page.getByTestId("confirm-cancel-open")).toBeDisabled();
  await page.getByTestId("retry-same").click();
  await expect(page.getByTestId("command-result")).toBeVisible();
  expect(mock.commands[0]?.request_id).toBe(mock.commands[1]?.request_id);
});

test("未捕获的 500 按结果未知处理", async ({ page }) => {
  await mockApi(page, {
    commandReply: () => ({ code: 500, body: { detail: "Internal Server Error" } }),
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("retry-same")).toBeVisible(); // 500：结果不确定
  await expect(page.getByTestId("command-error")).toContainText("结果未知");
});

test("409 明确是没有执行：不给同号重试，可以取消", async ({ page }) => {
  await mockApi(page, {
    commandReply: () => ({ code: 409, body: { error: "同一 request_id 的命令内容不同", retry_same_request_id: false } }),
  });
  await login(page);
  await page.getByTestId("nav-emergency").click();
  await page.getByTestId("open-cancel-open").click();
  await page.getByTestId("confirm-cancel-open").click();
  await expect(page.getByTestId("command-error")).toContainText("命令没有执行（服务端明确答复）");
  await expect(page.getByTestId("retry-same")).toHaveCount(0);
  await expect(page.getByTestId("cancel-cancel-open")).toBeVisible();
});

test("连接正常但状态无法识别时给出横幅", async ({ page }) => {
  await mockApi(page, { status: () => ({ code: 200, body: { ...PAPER, halt: null } }) });
  await login(page);
  await expect(page.getByTestId("mode-badge")).toHaveText("未知");
  await expect(page.getByTestId("untrusted-banner")).toBeVisible();
});
