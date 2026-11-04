// 总览、订单、持仓、风险（S-04 / S-13）。没有可信数据的地方一律显示“未知”或“暂无数据”，不显示旧值。
import { useEffect, useState } from "react";
import { call } from "../api/client";
import type { OrderList } from "../api/types.gen";
import type { StatusState } from "../status";
import { modeLabel } from "../status";
import { Empty, Notice, Sparkline, StatCard, Tag, statusTone } from "../ui";

const NO_SNAPSHOT = "等待 tradingd 发布账户快照";

export function connectionText(state: StatusState): string {
  switch (state.connection) {
    case "ok":
      return "连接正常";
    case "stale":
      return "数据过期";
    case "unauthorized":
      return "令牌无效";
    default:
      return "连接断开";
  }
}

function haltText(state: StatusState): string {
  const halt = state.connection === "ok" ? state.status?.halt ?? null : null;
  if (halt === null) return "未知";
  return halt.halted ? `已停止：${halt.reason ?? ""}` : "未停止";
}

export function Overview({ state }: { state: StatusState }) {
  const mode = modeLabel(state);
  return (
    <>
      <div className="page-title"><h2>总览</h2><span className="hint">每 3 秒刷新</span></div>
      <div className="cards">
        <StatCard label="模式" icon="check" value={mode} testId="overview-mode" />
        <StatCard label="服务器连接" icon="wifi" value={connectionText(state)} testId="overview-connection" />
        <StatCard label="急停" icon="stop" value={haltText(state)} testId="overview-halt" />
        <StatCard label="券商与数据健康" icon="info" value="未知" sub={<span className="hint">{NO_SNAPSHOT}</span>} />
      </div>
      <div className="cards section">
        <StatCard label="净值（USD）" value={null} />
        <StatCard label="现金（已结算 / 未结算）" value={null} />
        <StatCard label="当日盈亏" value={null} />
        <StatCard label="持仓数" value={null} />
      </div>
      <div className="section card">
        <div className="page-title"><h2>净值曲线</h2></div>
        <Sparkline points={[]} label="净值曲线" />
      </div>
      <p className="hint section">净值、现金、盈亏、持仓：{NO_SNAPSHOT}。</p>
    </>
  );
}

export function Orders() {
  const [orders, setOrders] = useState<OrderList | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = async () => {
      const result = await call<OrderList>("GET", "/v1/orders?limit=100");
      if (!alive) return;
      if (result.ok) {
        setOrders(result.data);
        setError(null);
      } else {
        setOrders(null); // 读不到就不显示旧列表
        setError(result.error);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);
  return (
    <>
      <div className="page-title"><h2>订单</h2><span className="hint">最近 100 笔，每 5 秒刷新</span></div>
      {orders === null ? (
        <div data-testid="orders-unknown">
          <Notice tone="warn" title="订单状态未知">
            {error ? `${error}。` : ""}服务器恢复后会自动刷新；在此之前请以券商端为准。
          </Notice>
        </div>
      ) : orders.orders.length === 0 ? (
        <div className="card"><Empty title="还没有订单" testId="orders-empty">策略或急停产生的订单会出现在这里</Empty></div>
      ) : (
        <div className="table-wrap">
          <table data-testid="orders-table">
            <thead>
              <tr>
                <th scope="col">意图</th><th scope="col">标的</th><th scope="col">方向</th>
                <th scope="col" className="num">数量</th><th scope="col" className="num">价格</th>
                <th scope="col">类别</th><th scope="col">状态</th><th scope="col">更新时间（UTC）</th>
              </tr>
            </thead>
            <tbody>
              {orders.orders.map((o) => (
                <tr key={o.intent_id}>
                  <td><code>{o.intent_id}</code></td>
                  <td>{o.symbol}</td>
                  <td>{o.side === "BUY" ? "买入" : "卖出"}</td>
                  <td className="num">{o.quantity}</td>
                  <td className="num">{o.limit_price ?? o.stop_price ?? "—"}</td>
                  <td>{o.order_class}</td>
                  <td><Tag tone={statusTone(o.status)}>{o.status ?? "未提交"}</Tag></td>
                  <td className="num">{o.updated_at?.replace("T", " ").slice(0, 19) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export function Positions() {
  return (
    <>
      <div className="page-title"><h2>持仓</h2></div>
      <div className="card"><Empty icon="positions" title="暂无持仓数据" testId="positions-empty">{NO_SNAPSHOT}</Empty></div>
    </>
  );
}

export function Risk({ state }: { state: StatusState }) {
  const status = state.connection === "ok" ? state.status : null;
  return (
    <>
      <div className="page-title"><h2>风险</h2></div>
      {status === null || status.halt === null ? (
        <div data-testid="risk-unknown">
          <Notice tone="warn" title="风险状态未知">连不上服务器或账本暂不可读。此时不要依据界面做决定，请以券商端为准。</Notice>
        </div>
      ) : (
        <div className="grid-2">
          <div className="card">
            <h3 style={{ marginBottom: "var(--s3)" }}>急停状态</h3>
            <dl className="kv" data-testid="risk-view">
              <dt>HALT</dt><dd>{status.halt.halted ? <Tag tone="danger">是</Tag> : <Tag tone="ok">否</Tag>}</dd>
              <dt>原因</dt><dd>{status.halt.reason ?? "—"}</dd>
              <dt>时间</dt><dd className="num" style={{ textAlign: "left" }}>{status.halt.since ?? "—"}</dd>
              <dt>操作者</dt><dd>{status.halt.set_by ?? "—"}</dd>
            </dl>
          </div>
          <div className="card">
            <h3 style={{ marginBottom: "var(--s3)" }}>敞口与保护性止损</h3>
            <Empty icon="risk" title="暂无数据">{NO_SNAPSHOT}</Empty>
          </div>
        </div>
      )}
    </>
  );
}
