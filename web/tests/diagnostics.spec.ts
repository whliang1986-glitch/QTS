// S-09c 端到端：诊断页。先预览全部内容，确认看过才能下载，下载的是预览过的那一份；只给 owner；失败和过期都讲清楚。
import { readFileSync } from "node:fs";
import { expect, test, type Page, type Route } from "@playwright/test";

type Json = Record<string, unknown>;
type Reply = { code: number; body: Json | null };

const PAPER: Json = {
  mode: "PAPER",
  halt: { halted: false, reason: null, since: null, set_by: null },
  changed_at: "2026-11-04T14:00:00.000000+00:00",
  changed_by: "dylan",
};

const PREVIEW: Json = {
  preview_id: "abc",
  created_at: "2026-11-06T12:00:00.000000+00:00",
  expires_in_seconds: 600,
  files: [
    { name: "manifest.json", content: '{"version": "1"}' },
    { name: "status.json", content: '{"mode": "PAPER", "account": "DU***567"}' },
  ],
  errors: {},
};

function failure(code: number, error: string): Reply {
  return { code, body: { error, retry_same_request_id: false } };
}

interface Mock {
  whoami: () => Reply;
  preview: (nth: number) => Reply | Promise<Reply> | "abort";
  archive: (nth: number) => Reply | Promise<Reply> | "abort" | "html";
  previews: number;
  archives: string[];
  archiveAuth: (string | undefined)[];
}

function json(route: Route, code: number, body: Json | null): Promise<void> {
  return route.fulfill({ status: code, contentType: "application/json", body: JSON.stringify(body) });
}

async function mockApi(page: Page, overrides: Partial<Mock> = {}): Promise<Mock> {
  const mock: Mock = {
    whoami: () => ({ code: 200, body: { name: "dylan", role: "owner" } }),
    preview: () => ({ code: 200, body: PREVIEW }),
    archive: () => ({ code: 200, body: null }),
    previews: 0,
    archives: [],
    archiveAuth: [],
    ...overrides,
  };
  await page.route("**/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.headers().authorization !== "Bearer test-token") {
      return json(route, 401, { error: "令牌无效", retry_same_request_id: false });
    }
    if (url.pathname === "/v1/whoami") {
      const { code, body } = mock.whoami();
      return json(route, code, body);
    }
    if (url.pathname === "/v1/status") return json(route, 200, PAPER);
    if (url.pathname === "/v1/diagnostics/previews") {
      mock.previews += 1;
      const reply = await mock.preview(mock.previews);
      if (reply === "abort") return route.abort();
      return json(route, reply.code, reply.body);
    }
    if (url.pathname.endsWith("/archive")) {
      mock.archives.push(url.pathname + url.search);
      mock.archiveAuth.push(request.headers().authorization);
      const reply = await mock.archive(mock.archives.length);
      if (reply === "abort") return route.abort();
      if (reply === "html") return route.fulfill({ status: 200, contentType: "text/html", body: "<html></html>" });
      if (reply.body !== null) return json(route, reply.code, reply.body);
      return route.fulfill({ status: 200, contentType: "application/zip", body: "PK-zip" }).catch(() => undefined);
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

async function open(page: Page): Promise<void> {
  await login(page);
  await expect(page.getByTestId("who")).toContainText("dylan");
  await page.getByTestId("nav-diagnostics").click();
}

/** 真实下载一次并等它完成（否则关闭浏览器时 Edge 会异常退出）。 */
async function downloadOnce(page: Page): Promise<string> {
  const saving = page.waitForEvent("download");
  await page.getByTestId("diag-download").click();
  const saved = await saving;
  expect(saved.suggestedFilename()).toBe("qts-diagnostics.zip");
  return readFileSync(await saved.path(), "utf8");
}

test("先预览全部内容，确认看过才能下载预览过的那一份", async ({ page }) => {
  const mock = await mockApi(page);
  await open(page);
  await expect(page.getByTestId("diag-generate")).toBeVisible();
  await page.waitForTimeout(200);
  expect(mock.previews).toBe(0); // 打开页面不生成
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-manifest.json")).toContainText('{"version": "1"}');
  await expect(page.getByTestId("diag-file-status.json")).toContainText("DU***567");
  expect(mock.previews).toBe(1);
  await expect(page.getByTestId("diag-download")).toBeDisabled();
  expect(mock.archives).toEqual([]);

  await page.getByTestId("diag-reviewed").check();
  expect(await downloadOnce(page)).toBe("PK-zip");
  await expect(page.getByTestId("diag-saved")).toBeVisible();
  expect(mock.archives).toEqual(["/v1/diagnostics/previews/abc/archive"]); // 令牌不进 URL
  expect(mock.archiveAuth).toEqual(["Bearer test-token"]);
});

test("重新生成后，下载的是新的那一份", async ({ page }) => {
  const mock = await mockApi(page, {
    preview: (nth) => ({ code: 200, body: { ...PREVIEW, preview_id: `p${nth}` } }),
  });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  await page.getByTestId("diag-generate").click();
  await expect.poll(() => mock.previews).toBe(2);
  await expect(page.getByTestId("diag-reviewed")).not.toBeChecked();
  await page.getByTestId("diag-reviewed").check();
  await downloadOnce(page);
  expect(mock.archives).toEqual(["/v1/diagnostics/previews/p2/archive"]);
});

test("只读令牌看不到生成按钮", async ({ page }) => {
  const mock = await mockApi(page, { whoami: () => ({ code: 200, body: { name: "dylan", role: "viewer" } }) });
  await open(page);
  await expect(page.getByTestId("diag-owner-only")).toBeVisible();
  await expect(page.getByTestId("diag-generate")).toHaveCount(0);
  expect(mock.previews).toBe(0);
});

test("身份没确认时只有说明，没有按钮", async ({ page }) => {
  let calls = 0;
  await mockApi(page, {
    whoami: () => {
      calls += 1;
      return calls === 1 ? { code: 200, body: { name: "dylan", role: "owner" } } : failure(503, "暂时不可用");
    },
  });
  await login(page); // 登录页确认过一次；进入外壳后身份取不到
  await page.getByTestId("nav-diagnostics").click();
  await expect(page.getByTestId("diag-owner-only")).toBeVisible();
  await expect(page.getByTestId("diag-generate")).toHaveCount(0);
});

test("生成中按钮禁用，连续点击只发一次", async ({ page }) => {
  let release = (): void => undefined;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const mock = await mockApi(page, {
    preview: async () => {
      await held;
      return { code: 200, body: PREVIEW };
    },
  });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-generate")).toBeDisabled();
  await page.getByTestId("diag-generate").click({ force: true }).catch(() => undefined);
  release();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  expect(mock.previews).toBe(1);
});

test("生成失败时说明原因，不显示内容，也不能下载", async ({ page }) => {
  await mockApi(page, { preview: () => failure(503, "无法生成诊断包") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-error")).toContainText("无法生成诊断包");
  await expect(page.getByTestId("diag-download")).toHaveCount(0);
});

test("生成被拒（403）、断网时都说明，不显示内容", async ({ page }) => {
  const replies: (Reply | "abort")[] = [failure(403, "只有 owner 能做这个操作"), "abort"];
  await mockApi(page, { preview: (nth) => replies[nth - 1] ?? "abort" });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-error")).toContainText("只有 owner");
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-error")).toContainText("连不上");
  await expect(page.getByTestId("diag-download")).toHaveCount(0);
});

test("生成时令牌失效（401）：回到登录页", async ({ page }) => {
  await mockApi(page, { preview: () => failure(401, "令牌无效") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("token-input")).toBeVisible();
});

test("回答格式不对：显示错误，页面不崩", async ({ page }) => {
  const replies: Reply[] = [
    { code: 200, body: {} },
    { code: 200, body: null },
    { code: 200, body: { ...PREVIEW, errors: { alerts: { x: 1 } } } }, // 渲染对象会让 React 报错、整个控制台白屏
  ];
  const mock = await mockApi(page, { preview: (nth) => replies[nth - 1] ?? replies[0]! });
  await open(page);
  for (const nth of [1, 2, 3]) {
    await page.getByTestId("diag-generate").click();
    await expect.poll(() => mock.previews).toBe(nth);
    await expect(page.getByTestId("diag-error")).toContainText("格式不对");
    await expect(page.getByTestId("diag-generate")).toBeEnabled();
  }
  await page.getByTestId("nav-emergency").click(); // 外壳还在
  await expect(page.getByTestId("mode-badge")).toBeVisible();
});

test("重新生成失败时，旧的那份不再显示", async ({ page }) => {
  await mockApi(page, { preview: (nth) => (nth === 1 ? { code: 200, body: PREVIEW } : failure(503, "无法生成诊断包")) });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-error")).toBeVisible();
  await expect(page.getByTestId("diag-file-status.json")).toHaveCount(0);
});

test("重新生成后要重新确认", async ({ page }) => {
  await mockApi(page);
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await expect(page.getByTestId("diag-download")).toBeEnabled();
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  await expect(page.getByTestId("diag-reviewed")).not.toBeChecked();
  await expect(page.getByTestId("diag-download")).toBeDisabled();
});

test("取不到的来源要显示出来", async ({ page }) => {
  await mockApi(page, { preview: () => ({ code: 200, body: { ...PREVIEW, errors: { alerts: "没有配置告警库" } } }) });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-source-errors")).toContainText("没有配置告警库");
});

test("服务端已过期（404）：内容清掉，提示重新生成", async ({ page }) => {
  await mockApi(page, { archive: () => failure(404, "这份诊断包不存在或已过期，请重新预览") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-expired")).toBeVisible();
  await expect(page.getByTestId("diag-download")).toHaveCount(0);
  await expect(page.getByTestId("diag-file-status.json")).toHaveCount(0);
});

test("到了有效期，内容清掉，也不能下载", async ({ page }) => {
  await mockApi(page, { preview: () => ({ code: 200, body: { ...PREVIEW, expires_in_seconds: 1 } }) });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-expired")).toBeVisible();
  await expect(page.getByTestId("diag-download")).toHaveCount(0);
  await expect(page.getByTestId("diag-file-status.json")).toHaveCount(0);
});

test("下载失败要说明，保留预览", async ({ page }) => {
  await mockApi(page, { archive: () => failure(503, "审计不可用") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toContainText("审计不可用");
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
});

test("先成功、再失败：不再显示已下载", async ({ page }) => {
  await mockApi(page, { archive: (nth) => (nth === 1 ? { code: 200, body: null } : failure(503, "审计不可用")) });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await downloadOnce(page);
  await expect(page.getByTestId("diag-saved")).toBeVisible();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toContainText("审计不可用");
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
});

test("下载时断网：显示错误，不显示已下载", async ({ page }) => {
  await mockApi(page, { archive: () => "abort" });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toBeVisible();
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
});

test("200 但不是 zip（代理的网页）：不存文件，显示错误", async ({ page }) => {
  await mockApi(page, { archive: () => "html" });
  await open(page);
  let downloads = 0;
  page.on("download", () => { downloads += 1; });
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toContainText("格式不对");
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
  expect(downloads).toBe(0);
});

test("下载时令牌失效（401）且状态轮询连不上：不等退出，内容立刻消失", async ({ page }) => {
  await mockApi(page, { archive: () => failure(401, "令牌无效") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  // 之后状态轮询一直连不上，不会触发退出：内容消失只能靠页面自己清
  await page.route("**/v1/status", (route) => route.abort());
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toBeVisible();
  await expect(page.getByTestId("diag-file-status.json")).toHaveCount(0);
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
  await expect(page.getByTestId("token-input")).toHaveCount(0); // 还在外壳里，不是靠退出才消失
});

test("下载被拒（403）：说明原因，不显示已下载", async ({ page }) => {
  await mockApi(page, { archive: () => failure(403, "只有 owner 能做这个操作") });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-error")).toContainText("只有 owner");
  await expect(page.getByTestId("diag-saved")).toHaveCount(0);
});

test("下载中两个按钮都禁用，连续点击只发一次", async ({ page }) => {
  let release = (): void => undefined;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const mock = await mockApi(page, {
    archive: async () => {
      await held;
      return { code: 200, body: null };
    },
  });
  await open(page);
  await page.getByTestId("diag-generate").click();
  await page.getByTestId("diag-reviewed").check();
  const saving = page.waitForEvent("download");
  await page.getByTestId("diag-download").click();
  await expect(page.getByTestId("diag-download")).toBeDisabled();
  await expect(page.getByTestId("diag-generate")).toBeDisabled();
  await page.getByTestId("diag-download").click({ force: true }).catch(() => undefined);
  release();
  await (await saving).path();
  await expect(page.getByTestId("diag-saved")).toBeVisible();
  expect(mock.archives).toHaveLength(1);
});

for (const how of ["离开页面", "退出登录"] as const) {
  test(`下载途中${how}：文件不落盘`, async ({ page }) => {
    let release = (): void => undefined;
    const held = new Promise<void>((resolve) => { release = resolve; });
    const mock = await mockApi(page, {
      archive: async () => {
        await held;
        return { code: 200, body: null };
      },
    });
    await open(page);
    let downloads = 0;
    page.on("download", () => { downloads += 1; });
    await page.getByTestId("diag-generate").click();
    await page.getByTestId("diag-reviewed").check();
    await page.getByTestId("diag-download").click();
    await expect.poll(() => mock.archives.length).toBe(1);
    if (how === "离开页面") await page.getByTestId("nav-overview").click();
    else await page.getByRole("button", { name: "退出" }).click();
    release();
    await page.waitForTimeout(500);
    expect(downloads).toBe(0);
  });
}

test("内容不进存储，离开页面、退出登录后都不保留", async ({ page }) => {
  await mockApi(page);
  await open(page);
  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  const stored = await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }, location.href]));
  expect(stored).not.toContain("DU***567");
  expect(stored).not.toContain("abc");

  await page.getByTestId("nav-overview").click();
  await page.getByTestId("nav-diagnostics").click();
  await expect(page.getByTestId("diag-file-status.json")).toHaveCount(0);

  await page.getByTestId("diag-generate").click();
  await expect(page.getByTestId("diag-file-status.json")).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByTestId("token-input")).toBeVisible();
  await expect(page.getByText("DU***567")).toHaveCount(0);
});
