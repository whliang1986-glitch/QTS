// 急停四个控件（V4.1 7.3：四个独立按钮，不合并）。每个都要先展开、再点“确认执行”；
// 撤保护性止损与紧急平仓是高危命令：apid 二次确认 + 手工输入 tradingd 的确认短语。
// 默认不撤保护性止损：只有第 3 个控件会撤止损。
// 命令状态在 App 级保存（commands.CommandStore）：结果未知的操作在面板关闭、切页后仍然保留并自动展开。
import { useState, type ReactNode } from "react";
import { OutcomeView, useCommand, type Params } from "../commands";

function Panel({ id, title, danger, pending, children }: {
  id: string;
  title: string;
  danger?: boolean;
  pending: boolean; // 有进行中或结果未知的操作：保持展开
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className={`control${danger ? " danger" : ""}`} data-testid={`control-${id}`}>
      <h3>{title}</h3>
      {open || pending ? (
        children(() => setOpen(false))
      ) : (
        <button type="button" onClick={() => setOpen(true)} data-testid={`open-${id}`}>
          {title}……
        </button>
      )}
    </section>
  );
}

function CommandForm({ id, name, high, params, ready, children, onDone }: {
  id: string;
  name: string;
  high: boolean;
  params: Params;
  ready: boolean;
  children?: ReactNode;
  onDone: () => void;
}) {
  const command = useCommand(id, high);
  const busy = command.outcome.kind === "running";
  const finished = command.outcome.kind === "done";
  return (
    <div>
      {children}
      {!finished ? (
        <button
          type="button"
          disabled={!ready || busy || command.attempt !== null}
          onClick={() => command.start(name, params)}
          data-testid={`confirm-${id}`}
        >
          确认执行
        </button>
      ) : null}
      {!command.unknown && !busy ? (
        <button type="button" onClick={() => { command.reset(); onDone(); }} data-testid={`cancel-${id}`}>
          {finished ? "关闭" : "取消"}
        </button>
      ) : null}
      <OutcomeView command={command} />
    </div>
  );
}

function usePending(id: string): boolean {
  const command = useCommand(id, false);
  return command.unknown || command.outcome.kind === "running";
}

function StopNewOrders() {
  const [reason, setReason] = useState("");
  return (
    <Panel id="stop" title="停止新增" pending={usePending("stop")}>
      {(close) => (
        <CommandForm id="stop" name="stop_new_orders" high={false}
          params={{ reason: reason.trim() }} ready={reason.trim() !== ""} onDone={close}>
          <p className="note">持久化 HALT：拒绝一切新增风险；不撤保护性止损。恢复须走恢复流程。</p>
          <label>
            原因（必填）
            <input value={reason} onChange={(e) => setReason(e.target.value)} data-testid="stop-reason" />
          </label>
        </CommandForm>
      )}
    </Panel>
  );
}

function CancelOpenOrders() {
  return (
    <Panel id="cancel-open" title="撤销开仓单" pending={usePending("cancel-open")}>
      {(close) => (
        <CommandForm id="cancel-open" name="cancel_open_orders" high={false} params={{}} ready onDone={close}>
          <p className="note">逐单撤销开仓单；减仓单与保护性止损不受影响。</p>
        </CommandForm>
      )}
    </Panel>
  );
}

function CancelProtectiveStops() {
  const [phrase, setPhrase] = useState("");
  return (
    <Panel id="cancel-stops" title="撤销保护性止损" danger pending={usePending("cancel-stops")}>
      {(close) => (
        <CommandForm id="cancel-stops" name="cancel_protective_stops" high
          params={{ confirmation: phrase }} ready={phrase !== ""} onDone={close}>
          <p className="warn">高危：撤掉后持仓失去止损保护。需要二次确认，并逐字输入确认短语（见运行手册）。</p>
          <label>
            确认短语
            <input value={phrase} onChange={(e) => setPhrase(e.target.value)} data-testid="stops-phrase" />
          </label>
        </CommandForm>
      )}
    </Panel>
  );
}

interface Preview {
  preview_id: string;
  confirmation: string;
  lines: { symbol: string; quantity: string; limit_price: string; max_slippage: string }[];
  warnings: string[];
}

function asPreview(detail: Record<string, unknown>): Preview | null {
  const { preview_id, confirmation, lines, warnings } = detail as Partial<Preview>;
  if (typeof preview_id !== "string" || typeof confirmation !== "string") return null;
  if (!Array.isArray(lines) || !Array.isArray(warnings)) return null;
  return { preview_id, confirmation, lines, warnings };
}

function EmergencyFlatten() {
  const previewCommand = useCommand("flatten-preview", false);
  const [phrase, setPhrase] = useState("");
  const done = previewCommand.outcome.kind === "done" ? previewCommand.outcome.response : null;
  const preview = done !== null && done.ok ? asPreview(done.detail) : null;
  const pending = usePending("flatten") || previewCommand.unknown || preview !== null;
  return (
    <Panel id="flatten" title="紧急平仓" danger pending={pending}>
      {(close) => (
        <div>
          <p className="warn">高危：按预览逐笔限价卖出全部持仓。先生成预览，再二次确认并输入预览给出的确认短语。</p>
          {preview === null ? (
            <>
              <button type="button" onClick={() => previewCommand.start("preview_flatten", {})}
                disabled={previewCommand.attempt !== null} data-testid="flatten-preview">
                生成平仓预览
              </button>
              {!previewCommand.unknown ? (
                <button type="button" onClick={() => { previewCommand.reset(); close(); }}>取消</button>
              ) : null}
              <OutcomeView command={previewCommand} />
            </>
          ) : (
            <CommandForm id="flatten" name="emergency_flatten" high
              params={{ preview_id: preview.preview_id, confirmation: phrase }}
              ready={phrase !== ""} onDone={() => { previewCommand.abandon(); setPhrase(""); close(); }}>
              <table data-testid="flatten-lines">
                <thead><tr><th>标的</th><th>数量</th><th>限价</th><th>最大滑点</th></tr></thead>
                <tbody>
                  {preview.lines.map((l) => (
                    <tr key={l.symbol}><td>{l.symbol}</td><td>{l.quantity}</td><td>{l.limit_price}</td><td>{l.max_slippage}</td></tr>
                  ))}
                </tbody>
              </table>
              {preview.warnings.map((w) => <p className="warn" key={w}>{w}</p>)}
              <p>请逐字输入确认短语：<code data-testid="flatten-expected">{preview.confirmation}</code></p>
              <input value={phrase} onChange={(e) => setPhrase(e.target.value)} data-testid="flatten-phrase" />
            </CommandForm>
          )}
        </div>
      )}
    </Panel>
  );
}

export function Emergency({ canCommand }: { canCommand: boolean }) {
  if (!canCommand) {
    return <p className="note" data-testid="viewer-note">当前令牌只读（viewer），或身份未确认，不能执行急停操作。</p>;
  }
  return (
    <div className="controls" data-testid="emergency-controls">
      <StopNewOrders />
      <CancelOpenOrders />
      <CancelProtectiveStops />
      <EmergencyFlatten />
    </div>
  );
}
