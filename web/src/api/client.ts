// apid 客户端（S-04）。类型全部来自 types.gen.ts（由 scripts/gen_api_types.py 生成，不手写）。
//
// - 令牌只放 sessionStorage：关闭页面即清除，不进 localStorage、不进 URL。
// - 每次调用返回成功或失败两种结果，调用方必须处理失败；不抛异常，不吞错误。
// - 每个请求都有超时：挂起的请求按失败处理（不让旧状态一直显示为“正常”）。
// - 失败里的 retrySame 为真时，只能用同一个 request_id 重试（服务端去重），界面不得换号。
import { translate } from "../i18n";
import { currentLang } from "../prefs";
import type { ErrorResponse } from "./types.gen";

const TOKEN_KEY = "qts.token";
export const QUERY_TIMEOUT_MS = 2500;
export const COMMAND_TIMEOUT_MS = 35000; // 比 apid 转发命令的超时（30 秒）略长

export function getToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token === null) sessionStorage.removeItem(TOKEN_KEY);
    else sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // 存储不可用：只在内存里用这一次（下次刷新需重新输入）
  }
}

export type Result<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: string; retrySame: boolean };

/** 命令的幂等键：每次用户发起操作生成一个，重试时沿用。 */
export function newRequestId(): string {
  return `web-${crypto.randomUUID()}`;
}

export async function call<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  timeoutMs: number = method === "GET" ? QUERY_TIMEOUT_MS : COMMAND_TIMEOUT_MS,
): Promise<Result<T>> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? null : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    // 网络断开或超时：没有拿到回答。对命令而言结果未知，只能同号重试。
    return { ok: false, status: 0, error: translate(currentLang(), "status.unreachable"), retrySame: true };
  }
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (response.status === 401) setToken(null); // 失效的令牌不留在存储里
  if (response.ok) return { ok: true, data: payload as T };
  const err = payload as Partial<ErrorResponse> | null;
  return {
    ok: false,
    status: response.status,
    error: typeof err?.error === "string" ? err.error : translate(currentLang(), "status.requestFailed", { status: response.status }),
    // 5xx 且回答里没有明确的 retry_same_request_id（未捕获的 500、代理 502 等）：结果不确定，按未知处理
    retrySame:
      err?.retry_same_request_id === true ||
      response.status === 504 ||
      (response.status >= 500 && typeof err?.retry_same_request_id !== "boolean"),
  };
}
