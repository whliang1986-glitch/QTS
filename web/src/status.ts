// 运行状态轮询（S-04）：串行轮询 /v1/status（上一次结束才发下一次），记录最后一次成功的时间。
//
// 界面据此判断“连接正常 / 数据过期 / 断开 / 未知”：
// - 每秒按 lastOkAt 检查一次：超过 STALE_MS 没有成功刷新就降为过期（不靠请求失败才发现）；
// - 断开、过期、账本读不了（503）或从未拿到过状态：模式显示“未知”，不拿旧值冒充当前值。
import { useEffect, useRef, useState } from "react";
import { call } from "./api/client";
import type { StatusView } from "./api/types.gen";
import { translate } from "./i18n";
import { currentLang } from "./prefs";

export const POLL_MS = 3000;
export const STALE_MS = 10000;

export type Connection = "ok" | "stale" | "down" | "unauthorized";

export interface StatusState {
  status: StatusView | null; // 只在数据新鲜时有值；失败或过期后置空（界面显示“未知”）
  connection: Connection;
  lastOkAt: number | null;
  error: string | null;
}

export function useStatus(onUnauthorized: () => void): StatusState {
  const [state, setState] = useState<StatusState>({
    status: null,
    connection: "down",
    lastOkAt: null,
    error: null,
  });
  const lastOk = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const tick = async () => {
      const result = await call<StatusView>("GET", "/v1/status");
      if (!alive) return;
      const now = Date.now();
      if (result.ok) {
        lastOk.current = now;
        setState({ status: result.data, connection: "ok", lastOkAt: now, error: null });
      } else if (result.status === 401) {
        setState({ status: null, connection: "unauthorized", lastOkAt: lastOk.current, error: result.error });
        onUnauthorized();
        return;
      } else {
        setState({
          status: null,
          connection: result.status === 0 ? "down" : "stale",
          lastOkAt: lastOk.current,
          error: result.error,
        });
      }
      timer = window.setTimeout(() => void tick(), POLL_MS); // 串行：不会有乱序的旧回答覆盖新结果
    };
    void tick();
    // 时钟检查：请求挂起（既不成功也不失败）时也能发现数据过期
    const watchdog = window.setInterval(() => {
      const last = lastOk.current;
      if (last !== null && Date.now() - last > STALE_MS) {
        setState((s) => (s.connection === "ok"
          ? { ...s, status: null, connection: "stale", error: translate(currentLang(), "status.staleWatchdog") }
          : s));
      }
    }, 1000);
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
      window.clearInterval(watchdog);
    };
  }, [onUnauthorized]);

  return state;
}

/** 模式标识只有三种：PAPER、HALT、未知。不可信或不认识的一律“未知”。 */
export function modeLabel(state: StatusState): "PAPER" | "HALT" | "UNKNOWN" {
  const status = state.status;
  if (state.connection !== "ok" || status === null) return "UNKNOWN";
  if (status.mode === "HALT" || status.halt?.halted === true) return "HALT";
  if (status.mode === "PAPER" && status.halt !== null && status.halt.halted === false) return "PAPER";
  return "UNKNOWN";
}
