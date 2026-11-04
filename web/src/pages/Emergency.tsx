// 急停四个控件（V4.1 7.3：四个独立按钮，不合并）。每个都要先展开、再点“确认执行”；
// 撤保护性止损与紧急平仓是高危命令：apid 二次确认 + 手工输入 tradingd 的确认短语。
// 默认不撤保护性止损：只有第 3 个控件会撤止损。
// 命令状态在 App 级保存（commands.CommandStore）：结果未知的操作在面板关闭、切页后仍然保留并自动展开。
// 视觉（S-13）：急停区单独隔开；高危控件红色描边，展开后“确认执行”才是实心红；每步写清“这会做什么”。
import { useState, type ReactNode } from "react";
import { OutcomeView, useCommand, type Params } from "../commands";
import { Button, Icon, Notice } from "../ui";

function Panel({ id, title, what, high, pending, children }: {
  id: string;
  title: string;
  what: string; // 这会做什么（收起时也显示）
  high?: boolean;
  pending: boolean; // 有进行中或结果未知的操作：保持展开
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const shown = open || pending;
  return (
    <section className={`control${high ? " high" : ""}${shown ? " open" : ""}`} data-testid={`control-${id}`}
      aria-labelledby={`title-${id}`}>
      <h3 id={`title-${id}`}>
        <Icon name={high ? "alert" : "stop"} />{title}
        {high ? <span className="tag danger">高危</span> : null}
      </h3>
      <p className="what">{what}</p>
      {shown ? (
        children(() => setOpen(false))
      ) : (
        <div className="actions">
          <Button kind={high ? "danger-outline" : "default"} onClick={() => setOpen(true)} data-testid={`open-${id}`}>
            {title}……
          </Button>
        </div>
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
    <div className="stack" style={{ gap: "var(--s2)" }}>
      {children}
      <div className="actions">
        {!finished ? (
          <Button
            kind={high ? "danger" : "primary"}
            disabled={!ready || busy || command.attempt !== null}
            onClick={() => command.start(name, params)}
            data-testid={`confirm-${id}`}
          >
            确认执行
          </Button>
        ) : null}
        {!command.unknown && !busy ? (
          <Button kind="ghost" onClick={() => { command.reset(); onDone(); }} data-testid={`cancel-${id}`}>
            {finished ? "关闭" : "取消"}
          </Button>
        ) : null}
      </div>
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
    <Panel id="stop" title="停止新增" pending={usePending("stop")}
      what="持久化 HALT：拒绝一切新增风险。不撤已有订单，也不撤保护性止损。恢复须走恢复流程。">
      {(close) => (
        <CommandForm id="stop" name="stop_new_orders" high={false}
          params={{ reason: reason.trim() }} ready={reason.trim() !== ""} onDone={close}>
          <label className="field">
            原因（必填，会写进审计）
            <input className="input" value={reason} onChange={(e) => setReason(e.target.value)} data-testid="stop-reason" />
          </label>
        </CommandForm>
      )}
    </Panel>
  );
}

function CancelOpenOrders() {
  return (
    <Panel id="cancel-open" title="撤销开仓单" pending={usePending("cancel-open")}
      what="逐单撤销尚未成交的开仓单与减仓单（含紧急平仓单）。保护性止损不受影响。">
      {(close) => (
        <CommandForm id="cancel-open" name="cancel_open_orders" high={false} params={{}} ready onDone={close} />
      )}
    </Panel>
  );
}

function CancelProtectiveStops() {
  const [phrase, setPhrase] = useState("");
  return (
    <Panel id="cancel-stops" title="撤销保护性止损" high pending={usePending("cancel-stops")}
      what="撤掉全部保护性止损：之后持仓没有止损保护。需要二次确认，并逐字输入确认短语。">
      {(close) => (
        <CommandForm id="cancel-stops" name="cancel_protective_stops" high
          params={{ confirmation: phrase }} ready={phrase !== ""} onDone={close}>
          <Notice tone="danger" title="这会让持仓失去止损保护">只在止损本身出错、需要人工接管时使用。确认短语见运行手册。</Notice>
          <label className="field">
            确认短语
            <input className="input" value={phrase} onChange={(e) => setPhrase(e.target.value)} data-testid="stops-phrase" />
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
    <Panel id="flatten" title="紧急平仓" high pending={pending}
      what="按预览逐笔限价卖出全部持仓（扣除已挂的减仓单）。仍挂着保护性止损的标的会被拒绝，须先撤止损。先生成预览（不下单），核对后再二次确认并输入确认短语。">
      {(close) => (
        <div className="stack" style={{ gap: "var(--s2)" }}>
          {preview === null ? (
            <>
              <div className="actions">
                <Button onClick={() => previewCommand.start("preview_flatten", {})}
                  disabled={previewCommand.attempt !== null} data-testid="flatten-preview">
                  生成平仓预览（不下单）
                </Button>
                {!previewCommand.unknown ? (
                  <Button kind="ghost" onClick={() => { previewCommand.reset(); close(); }}>取消</Button>
                ) : null}
              </div>
              <OutcomeView command={previewCommand} />
            </>
          ) : (
            <CommandForm id="flatten" name="emergency_flatten" high
              params={{ preview_id: preview.preview_id, confirmation: phrase }}
              ready={phrase !== ""} onDone={() => { previewCommand.abandon(); setPhrase(""); close(); }}>
              <div className="table-wrap">
                <table data-testid="flatten-lines">
                  <thead><tr><th scope="col">标的</th><th scope="col" className="num">数量</th>
                    <th scope="col" className="num">限价</th><th scope="col" className="num">最大滑点</th></tr></thead>
                  <tbody>
                    {preview.lines.map((l) => (
                      <tr key={l.symbol}><td>{l.symbol}</td><td className="num">{l.quantity}</td>
                        <td className="num">{l.limit_price}</td><td className="num">{l.max_slippage}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {preview.warnings.map((w) => <Notice tone="warn" title={w} key={w} />)}
              <p className="muted">请逐字输入确认短语：<code data-testid="flatten-expected">{preview.confirmation}</code></p>
              <input className="input" aria-label="平仓确认短语" value={phrase}
                onChange={(e) => setPhrase(e.target.value)} data-testid="flatten-phrase" />
            </CommandForm>
          )}
        </div>
      )}
    </Panel>
  );
}

export function Emergency({ canCommand }: { canCommand: boolean }) {
  return (
    <>
      <div className="page-title"><h2>急停</h2><span className="hint">四个控件相互独立；每个都要展开后再确认</span></div>
      {!canCommand ? (
        <div data-testid="viewer-note">
          <Notice tone="info" title="当前令牌只读">viewer 令牌或身份尚未确认时，不能执行急停操作。</Notice>
        </div>
      ) : (
        <div className="danger-zone">
          <header><Icon name="stop" /><h2>急停区</h2></header>
          <div className="controls" data-testid="emergency-controls">
            <StopNewOrders />
            <CancelOpenOrders />
            <CancelProtectiveStops />
            <EmergencyFlatten />
          </div>
        </div>
      )}
    </>
  );
}
