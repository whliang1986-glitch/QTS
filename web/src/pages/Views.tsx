// 总览、订单、持仓、风险（S-04 / S-13）。没有可信数据的地方一律显示“未知”或“暂无数据”，不显示旧值。
import { useEffect, useState } from "react";
import { call } from "../api/client";
import type { OrderList } from "../api/types.gen";
import type { StatusState } from "../status";
import { modeLabel } from "../status";
import { type MessageKey, type T, useT } from "../i18n";
import { Empty, Notice, Sparkline, StatCard, Tag, statusTone, useModeText } from "../ui";

export function connectionText(state: StatusState): MessageKey {
  switch (state.connection) {
    case "ok":
      return "conn.ok";
    case "stale":
      return "conn.stale";
    case "unauthorized":
      return "conn.unauthorized";
    default:
      return "conn.down";
  }
}

function haltText(state: StatusState, t: T): string {
  const halt = state.connection === "ok" ? state.status?.halt ?? null : null;
  if (halt === null) return t("common.unknown");
  return halt.halted ? t("overview.haltOn", { reason: halt.reason ?? "" }) : t("overview.haltOff");
}

export function Overview({ state }: { state: StatusState }) {
  const t = useT();
  const mode = useModeText()(modeLabel(state));
  return (
    <>
      <div className="page-title"><h2>{t("overview.title")}</h2><span className="hint">{t("overview.refresh")}</span></div>
      <div className="cards">
        <StatCard label={t("overview.mode")} icon="check" value={mode} testId="overview-mode" />
        <StatCard label={t("overview.connection")} icon="wifi" value={t(connectionText(state))} testId="overview-connection" />
        <StatCard label={t("overview.halt")} icon="stop" value={haltText(state, t)} testId="overview-halt" />
        <StatCard label={t("overview.health")} icon="info" value={t("common.unknown")} sub={<span className="hint">{t("common.noSnapshot")}</span>} />
      </div>
      <div className="cards section">
        <StatCard label={t("overview.nav")} value={null} />
        <StatCard label={t("overview.cash")} value={null} />
        <StatCard label={t("overview.pnl")} value={null} />
        <StatCard label={t("overview.positions")} value={null} />
      </div>
      <div className="section card">
        <div className="page-title"><h2>{t("overview.curve")}</h2></div>
        <Sparkline points={[]} label={t("overview.curve")} />
      </div>
      <p className="hint section">{t("overview.footer", { reason: t("common.noSnapshot") })}</p>
    </>
  );
}

export function Orders() {
  const t = useT();
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
      <div className="page-title"><h2>{t("orders.title")}</h2><span className="hint">{t("orders.refresh")}</span></div>
      {orders === null ? (
        <div data-testid="orders-unknown">
          <Notice tone="warn" title={t("orders.unknown")}>
            {error ? `${error} · ` : ""}{t("orders.unknownHint")}
          </Notice>
        </div>
      ) : orders.orders.length === 0 ? (
        <div className="card"><Empty title={t("orders.empty")} testId="orders-empty">{t("orders.emptyHint")}</Empty></div>
      ) : (
        <div className="table-wrap">
          <table data-testid="orders-table">
            <thead>
              <tr>
                <th scope="col">{t("orders.col.intent")}</th><th scope="col">{t("orders.col.symbol")}</th><th scope="col">{t("orders.col.side")}</th>
                <th scope="col" className="num">{t("orders.col.qty")}</th><th scope="col" className="num">{t("orders.col.price")}</th>
                <th scope="col">{t("orders.col.class")}</th><th scope="col">{t("orders.col.status")}</th><th scope="col">{t("orders.col.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {orders.orders.map((o) => (
                <tr key={o.intent_id}>
                  <td><code>{o.intent_id}</code></td>
                  <td>{o.symbol}</td>
                  <td>{o.side === "BUY" ? t("common.buy") : t("common.sell")}</td>
                  <td className="num">{o.quantity}</td>
                  <td className="num">{o.limit_price ?? o.stop_price ?? "—"}</td>
                  <td>{o.order_class}</td>
                  <td><Tag tone={statusTone(o.status)}>{o.status ?? t("common.notSubmitted")}</Tag></td>
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
  const t = useT();
  return (
    <>
      <div className="page-title"><h2>{t("positions.title")}</h2></div>
      <div className="card"><Empty icon="positions" title={t("positions.empty")} testId="positions-empty">{t("common.noSnapshot")}</Empty></div>
    </>
  );
}

export function Risk({ state }: { state: StatusState }) {
  const t = useT();
  const status = state.connection === "ok" ? state.status : null;
  return (
    <>
      <div className="page-title"><h2>{t("risk.title")}</h2></div>
      {status === null || status.halt === null ? (
        <div data-testid="risk-unknown">
          <Notice tone="warn" title={t("risk.unknown")}>{t("risk.unknownHint")}</Notice>
        </div>
      ) : (
        <div className="grid-2">
          <div className="card">
            <h3 style={{ marginBottom: "var(--s3)" }}>{t("risk.haltState")}</h3>
            <dl className="kv" data-testid="risk-view">
              <dt>HALT</dt><dd>{status.halt.halted ? <Tag tone="danger">{t("common.yes")}</Tag> : <Tag tone="ok">{t("common.no")}</Tag>}</dd>
              <dt>{t("risk.reason")}</dt><dd>{status.halt.reason ?? "—"}</dd>
              <dt>{t("risk.time")}</dt><dd className="num" style={{ textAlign: "left" }}>{status.halt.since ?? "—"}</dd>
              <dt>{t("risk.actor")}</dt><dd>{status.halt.set_by ?? "—"}</dd>
            </dl>
          </div>
          <div className="card">
            <h3 style={{ marginBottom: "var(--s3)" }}>{t("risk.exposure")}</h3>
            <Empty icon="risk" title={t("common.noData")}>{t("common.noSnapshot")}</Empty>
          </div>
        </div>
      )}
    </>
  );
}
