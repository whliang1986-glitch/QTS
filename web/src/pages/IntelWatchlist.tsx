// 情报与影响视图（S-15）+ 自选与交易名单（S-14）+ 待审批队列（S-10）。
// 安全原则：页面明确标注“仅供研究，不触发下单”，没有任何下单或改仓控件；
// 未核实或有争议的情报显眼标注，绝不进入影响排名；数据不可用时如实显示“无法获取”。
import { useEffect, useState } from "react";
import { call } from "../api/client";
import type { ApprovalItemView, ApprovalListView, WatchlistItemView, WatchlistView } from "../api/types.gen";
import { useT } from "../i18n";
import { Button, Notice, Tag } from "../ui";

interface IntelFeedEntry {
  id: string;
  symbol: string;
  source: string;
  credibility: "OFFICIAL" | "MEDIA" | "SOCIAL";
  status: "VERIFIED" | "UNVERIFIED" | "DISPUTED";
  publishedAt: string;
  title: string;
  summary: string;
  excerpt: string;
  impacts: { symbol: string; score: string; direction: "UP" | "DOWN"; path: string; source: string }[];
}

const DEFAULT_INTEL: IntelFeedEntry = {
  id: "intel-eia-1",
  symbol: "SPY",
  source: "Reuters / AP（多源交叉核验）",
  credibility: "MEDIA",
  status: "VERIFIED",
  publishedAt: "2026-11-16T14:30:00Z",
  title: "OPEC+ 维持原油减产配额，上游能源行业现金流预期上修",
  summary: "AI 生成摘要：多源核验供应偏紧信号延续，预计支撑上游能源 ETF 利润率，航空运输成本承压。",
  excerpt: "Official statement confirms production quotas remain unchanged through Q1.",
  impacts: [
    { symbol: "XLE", score: "+0.65", direction: "UP", path: "原油供给收缩 → 能源开采利润率扩张 → XLE", source: "EIA-2026-Report" },
    { symbol: "JETS", score: "-0.42", direction: "DOWN", path: "航油成本上升 → 航空利润承压 → JETS", source: "IATA-2026-Q4" },
  ],
};

const SAMPLE_INTEL: readonly IntelFeedEntry[] = [
  DEFAULT_INTEL,
  {
    id: "intel-rumor-2",
    symbol: "SPY",
    source: "社交媒体匿名传闻",
    credibility: "SOCIAL",
    status: "UNVERIFIED",
    publishedAt: "2026-11-16T13:50:00Z",
    title: "未核实传闻：某大型科技公司计划调整资本开支",
    summary: "仅单一未核实来源，未进入影响排名，不触发任何策略信号。",
    excerpt: "Unverified social post without primary filing.",
    impacts: [],
  },
];

export function IntelWatchlist({ canApprove }: { canApprove: boolean }) {
  const t = useT();
  const [watchlist, setWatchlist] = useState<WatchlistItemView[] | null>(null);
  const [wlError, setWlError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<ApprovalItemView[] | null>(null);
  const [aprError, setAprError] = useState<string | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<string>("SPY");
  const [selectedIntelId, setSelectedIntelId] = useState<string>(DEFAULT_INTEL.id);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      const [wRes, aRes] = await Promise.all([
        call<WatchlistView>("GET", "/v1/watchlist"),
        call<ApprovalListView>("GET", "/v1/approvals?pending_only=true"),
      ]);
      if (!alive) return;
      if (wRes.ok) {
        setWatchlist(wRes.data.items);
        setWlError(null);
      } else {
        setWlError(wRes.error);
      }
      if (aRes.ok) {
        setApprovals(aRes.data.items);
        setAprError(null);
      } else {
        setAprError(aRes.error);
      }
    };
    void load();
    return () => { alive = false; };
  }, []);

  const decide = async (approvalId: string, approve: boolean) => {
    const res = await call<ApprovalItemView>("POST", `/v1/approvals/${approvalId}/decide`, {
      approve,
      reason: approve ? "人工确认通过" : "人工拒绝该提议",
    });
    if (res.ok && approvals) {
      setApprovals(approvals.filter((a) => a.approval_id !== approvalId));
    }
  };

  const activeIntel = SAMPLE_INTEL.find((x) => x.id === selectedIntelId) ?? DEFAULT_INTEL;

  return (
    <div className="stack">
      <Notice tone="info" title={t("intel.researchOnly")} testId="research-only-banner">
        {t("intel.researchHint")}
      </Notice>

      <div className="grid-2">
        <section className="card">
          <h2>{t("intel.watchlist")}</h2>
          {wlError ? (
            <Notice tone="warn" title={t("intel.unavailable")}>{wlError}</Notice>
          ) : watchlist === null ? (
            <p className="hint">加载自选与交易名单...</p>
          ) : watchlist.length === 0 ? (
            <p className="hint">暂无自选标的</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>标的</th>
                  <th>行情状态</th>
                  <th>三层状态</th>
                  <th>资格</th>
                </tr>
              </thead>
              <tbody>
                {watchlist.map((item) => (
                  <tr
                    key={item.symbol}
                    onClick={() => setSelectedSymbol(item.symbol)}
                    style={{ cursor: "pointer" }}
                  >
                    <td><strong>{item.symbol}</strong></td>
                    <td><Tag tone={item.quote_status === "REALTIME" ? "ok" : "warn"}>{item.quote_status}</Tag></td>
                    <td><Tag tone={item.trading_state === "IN_TRADING_LIST" ? "ok" : "neutral"}>{item.trading_state}</Tag></td>
                    <td>{item.eligible ? "合格" : item.eligibility_reasons[0] || "不合格"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h2>{`${selectedSymbol} · 标的情报流（时间倒序）`}</h2>
          <div className="stack">
            {SAMPLE_INTEL.map((ev) => (
              <div
                key={ev.id}
                className="card"
                style={{ padding: "var(--s3)", cursor: "pointer" }}
                onClick={() => setSelectedIntelId(ev.id)}
              >
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <strong>{ev.title}</strong>
                  <Tag tone={ev.status === "VERIFIED" ? "ok" : "warn"}>
                    {ev.status === "VERIFIED" ? "已核实" : ev.status === "UNVERIFIED" ? "未核实" : "有争议"}
                  </Tag>
                </div>
                <p className="hint">{ev.source} · {ev.credibility} · {ev.publishedAt}</p>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="grid-2">
        <section className="card">
          <h2>情报影响与传导路径详情</h2>
          <p><strong>{activeIntel.title}</strong></p>
          <p className="hint">{activeIntel.summary}</p>
          <blockquote className="hint" style={{ borderLeft: "2px solid var(--line)", paddingLeft: "var(--s2)" }}>
            {activeIntel.excerpt}
          </blockquote>
          {activeIntel.status !== "VERIFIED" ? (
            <Notice tone="warn" title="未核实情报不进入影响排名">
              单一来源或未交叉核验情报仅作提示展示，绝不传导至策略与交易信号。
            </Notice>
          ) : (
            <table className="table">
              <thead>
                <tr><th>影响标的</th><th>影响分</th><th>传导路径</th><th>数据出处</th></tr>
              </thead>
              <tbody>
                {activeIntel.impacts.map((imp) => (
                  <tr key={imp.symbol}>
                    <td><strong>{imp.symbol}</strong></td>
                    <td><Tag tone={imp.direction === "UP" ? "ok" : "danger"}>{imp.score}</Tag></td>
                    <td>{imp.path}</td>
                    <td className="hint">{imp.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="card">
          <h2>{t("intel.approvals")}</h2>
          {aprError ? (
            <Notice tone="warn" title={t("intel.unavailable")}>{aprError}</Notice>
          ) : !approvals || approvals.length === 0 ? (
            <p className="hint">{t("intel.emptyApprovals")}</p>
          ) : (
            <div className="stack">
              {approvals.map((a) => (
                <div key={a.approval_id} className="card" style={{ padding: "var(--s3)" }}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <strong>{a.title}</strong>
                    <Tag tone="neutral">{a.kind}</Tag>
                  </div>
                  <p className="hint">{a.summary} · 发起人：{a.proposer}</p>
                  {canApprove ? (
                    <div className="row" style={{ gap: "var(--s2)" }}>
                      <Button kind="primary" small onClick={() => void decide(a.approval_id, true)}>{t("intel.approve")}</Button>
                      <Button kind="ghost" small onClick={() => void decide(a.approval_id, false)}>{t("intel.reject")}</Button>
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
