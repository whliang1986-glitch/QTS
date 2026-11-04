// 命令执行（S-04）：一次用户操作 = 一个 request_id；结果未知时只允许用同一编号重试。
//
// - 命令状态放在 App 级的 CommandStore 里（按控件区分）：关闭面板、切换页面都不会丢掉编号。
//   刷新页面会丢（内存里），运行手册要求刷新前先查看状态。
// - “结果未知”是粘性的：一旦出现 504、网络断开或超时、或服务端要求同号重试，之后任何失败都保留
//   同号重试入口；要开始新的操作，必须显式“放弃”（界面提示可能已经执行）。
// - 高危命令（tier = high）先向 apid 申请二次确认号，再连同 tradingd 的确认短语一起提交（S-03）。
//   确认号过期（403）时重新申请确认号，request_id 不变。
import { createContext, useContext, useState, type ReactNode } from "react";
import { call, newRequestId, type Result } from "./api/client";
import type { CommandResponse, ConfirmationResponse } from "./api/types.gen";
import { Button, Notice } from "./ui";

export type Params = Record<string, string>;

export interface Attempt {
  requestId: string;
  name: string;
  params: Params;
  confirmationId: string | null;
  unknown: boolean; // 曾经结果未知：只能同号重试，不能换号
}

export type Outcome =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; response: CommandResponse }
  | { kind: "failed"; error: string; retrySame: boolean };

interface Entry {
  attempt: Attempt | null;
  outcome: Outcome;
}

type Store = [Record<string, Entry>, (key: string, entry: Entry) => void];

const EMPTY: Entry = { attempt: null, outcome: { kind: "idle" } };
const CommandContext = createContext<Store | null>(null);

export function CommandStore({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<Record<string, Entry>>({});
  const put = (key: string, entry: Entry) => setEntries((all) => ({ ...all, [key]: entry }));
  return <CommandContext.Provider value={[entries, put]}>{children}</CommandContext.Provider>;
}

async function confirmation(name: string, params: Params): Promise<Result<ConfirmationResponse>> {
  return call<ConfirmationResponse>("POST", "/v1/commands/confirmations", { name, params });
}

async function send(attempt: Attempt): Promise<Result<CommandResponse>> {
  const body: Record<string, unknown> = {
    request_id: attempt.requestId,
    name: attempt.name,
    params: attempt.params,
  };
  if (attempt.confirmationId !== null) body.confirmation_id = attempt.confirmationId;
  return call<CommandResponse>("POST", "/v1/commands", body);
}

export function useCommand(key: string, high: boolean) {
  const store = useContext(CommandContext);
  if (store === null) throw new Error("useCommand 必须在 CommandStore 内使用");
  const [entries, put] = store;
  const entry = entries[key] ?? EMPTY;

  const fail = (attempt: Attempt, error: string, retrySame: boolean) => {
    const unknown = attempt.unknown || retrySame;
    put(key, { attempt: { ...attempt, unknown }, outcome: { kind: "failed", error, retrySame: unknown } });
  };

  const run = async (current: Attempt) => {
    put(key, { attempt: current, outcome: { kind: "running" } });
    let next = current;
    if (high && next.confirmationId === null) {
      const confirm = await confirmation(next.name, next.params);
      if (!confirm.ok) {
        fail(next, confirm.error, next.unknown); // 申请确认号失败：命令没有发出
        return;
      }
      next = { ...next, confirmationId: confirm.data.confirmation_id };
    }
    const result = await send(next);
    if (result.ok) {
      put(key, { attempt: next, outcome: { kind: "done", response: result.data } });
      return;
    }
    if (result.status === 403 && high) next = { ...next, confirmationId: null }; // 下次重新申请
    fail(next, result.error, result.retrySame);
  };

  const attempt = entry.attempt;
  return {
    attempt,
    outcome: entry.outcome,
    unknown: attempt?.unknown === true,
    start: (name: string, params: Params) => {
      if (attempt?.unknown) return; // 上一次结果未知：不能换号重发
      void run({ requestId: newRequestId(), name, params, confirmationId: null, unknown: false });
    },
    retry: () => {
      if (attempt !== null) void run(attempt);
    },
    /** 结束这次操作。结果未知时不能直接结束，须用 abandon 显式放弃。 */
    reset: () => {
      if (!attempt?.unknown) put(key, EMPTY);
    },
    abandon: () => put(key, EMPTY),
  };
}

export function OutcomeView({ command }: { command: ReturnType<typeof useCommand> }) {
  const { outcome, attempt } = command;
  if (outcome.kind === "idle") return null;
  if (outcome.kind === "running") {
    return <div data-testid="command-running"><Notice tone="info" title="执行中……">正在等待 tradingd 回答。</Notice></div>;
  }
  if (outcome.kind === "done") {
    const r = outcome.response;
    const reason = !r.ok && typeof r.detail.error === "string" ? r.detail.error : null;
    return (
      <div data-testid="command-result">
        <Notice tone={r.ok ? "ok" : "warn"} title={`${r.ok ? "已执行" : "tradingd 拒绝执行"}${r.replayed ? "（这是同一编号之前那次的结果）" : ""}`}>
          {reason ? `原因：${reason}` : null}
        </Notice>
      </div>
    );
  }
  return (
    <div data-testid="command-error">
      <Notice
        tone={outcome.retrySame ? "warn" : "danger"}
        title={outcome.error}
        action={outcome.retrySame ? (
          <>
            <Button kind="primary" small onClick={command.retry} data-testid="retry-same">
              用同一编号重试（{attempt?.requestId}）
            </Button>
            <Button kind="ghost" small onClick={command.abandon} data-testid="abandon">
              放弃这次操作（我已查看状态，知道它可能已执行）
            </Button>
          </>
        ) : null}
      >
        {outcome.retrySame
          ? "结果未知：命令可能已经执行。请用同一编号重试，tradingd 会返回原结果，不会执行两次。"
          : "命令没有执行（服务端明确答复）。请按提示修正后，取消并重新操作。"}
      </Notice>
    </div>
  );
}
