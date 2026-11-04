// 总览、订单、持仓、风险（S-04）。没有可信数据的地方一律显示“未知”或“暂无数据”，不显示旧值。
import { useEffect, useState } from "react";
import { call } from "../api/client";
import type { OrderList } from "../api/types.gen";
import type { StatusState } from "../status";
import { modeLabel } from "../status";

const NO_SNAPSHOT = "暂无数据（等待 tradingd 发布账户快照）";

export function Overview({ state }: { state: StatusState }) {
  const halt = state.connection === "ok" ? state.status?.halt ?? null : null;
  return (
    <div className="grid">
      <Card title="模式" value={modeLabel(state)} testId="overview-mode" />
      <Card title="服务器连接" value={connectionText(state)} testId="overview-connection" />
      <Card title="急停" value={halt === null ? "未知" : halt.halted ? `已停止：${halt.reason ?? ""}` : "未停止"} testId="overview-halt" />
      <Card title="净值" value={NO_SNAPSHOT} />
      <Card title="现金（已结算 / 未结算）" value={NO_SNAPSHOT} />
      <Card title="当日盈亏" value={NO_SNAPSHOT} />
      <Card title="券商连接与数据健康" value={NO_SNAPSHOT} />
    </div>
  );
}

function Card({ title, value, testId }: { title: string; value: string; testId?: string }) {
  return (
    <div className="card">
      <div className="card-title">{title}</div>
      <div className="card-value" data-testid={testId}>{value}</div>
    </div>
  );
}

export function connectionText(state: StatusState): string {
  switch (state.connection) {
    case "ok":
      return "正常";
    case "stale":
      return "数据过期";
    case "unauthorized":
      return "令牌无效";
    default:
      return "断开";
  }
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
  if (orders === null) {
    return <p className="warn" data-testid="orders-unknown">订单状态未知{error ? `：${error}` : ""}</p>;
  }
  if (orders.orders.length === 0) return <p data-testid="orders-empty">还没有订单</p>;
  return (
    <table data-testid="orders-table">
      <thead>
        <tr><th>意图</th><th>标的</th><th>方向</th><th>数量</th><th>限价</th><th>类别</th><th>状态</th><th>更新时间</th></tr>
      </thead>
      <tbody>
        {orders.orders.map((o) => (
          <tr key={o.intent_id}>
            <td>{o.intent_id}</td><td>{o.symbol}</td><td>{o.side}</td><td>{o.quantity}</td>
            <td>{o.limit_price ?? o.stop_price ?? ""}</td><td>{o.order_class}</td>
            <td>{o.status ?? "未提交"}</td><td>{o.updated_at ?? ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Positions() {
  return <p className="note" data-testid="positions-empty">{NO_SNAPSHOT}</p>;
}

export function Risk({ state }: { state: StatusState }) {
  const status = state.connection === "ok" ? state.status : null;
  if (status === null || status.halt === null) {
    return <p className="warn" data-testid="risk-unknown">风险状态未知</p>;
  }
  return (
    <dl data-testid="risk-view">
      <dt>HALT</dt><dd>{status.halt.halted ? "是" : "否"}</dd>
      <dt>原因</dt><dd>{status.halt.reason ?? "—"}</dd>
      <dt>时间</dt><dd>{status.halt.since ?? "—"}</dd>
      <dt>操作者</dt><dd>{status.halt.set_by ?? "—"}</dd>
      <dt>敞口、集中度、保护性止损覆盖</dt><dd>{NO_SNAPSHOT}</dd>
    </dl>
  );
}
