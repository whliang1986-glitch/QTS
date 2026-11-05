// AI 指挥中枢布局（UI-02 / UI-03 原型）。按《科幻操作面板 UI 规范 V1.0》第 3、4 节分区：
// 顶部系统栏 → 主导航 → 左：市场概览 / 中：指挥中枢（决策分析 ⇄ 全球情报）/ 右：账户 → 底部信息带。
//
// 安全约束（规范第 8、9 节，S-16 原则 2）：
// - 模式徽章、DEMO 标记、HALT、数据过期提示保持静态，不加动效；
// - 每个区块要么显示后端给的数据（附数据时间与来源），要么显示“未接入 / 未启用”，不生成数字；
// - 这里没有任何下单、改仓或改风控的控件；急停入口仍是现有的急停页（二次确认、审计）。
import { useState } from "react";
import { WorldMap } from "./WorldMap";
import type {
  ActivityKind, Avail, ConsoleData, Health, IntelEvent, Severity, Slice,
} from "./types";

const NAV = ["总览", "市场分析", "AI 指挥中枢", "策略管理", "交易执行", "风险控制", "数据中心", "系统设置"];

const KIND_TEXT: Record<ActivityKind, string> = {
  SUGGESTION: "策略建议", // AI 上线后由后端字段区分来源（策略或 AI）
  RISK_PASSED: "风控通过",
  SUBMITTED: "已提交券商",
  FILLED: "已成交",
  DATA: "数据更新",
  MANUAL: "人工操作",
};
const HEALTH_TEXT: Record<Health, string> = {
  OK: "正常", WARN: "警告", DEGRADED: "降级", DOWN: "故障", UNKNOWN: "未知",
};
const SEVERITY_TEXT: Record<Severity, string> = {
  CRITICAL: "重大", HIGH: "重要", MEDIUM: "中", LOW: "低",
};

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York",
  });
}

function Panel({ title, en, children, source, testId }: {
  title: string; en: string; children: React.ReactNode; source?: Avail<unknown>; testId?: string;
}) {
  return (
    <section className="cx-panel" data-testid={testId} aria-label={title}>
      <header className="cx-panel-head">
        <h2>{title}<span className="cx-en">{en}</span></h2>
        {source?.state === "ok" ? (
          <span className="cx-meta">{source.source} · {hhmm(source.asOf)} 美东</span>
        ) : null}
      </header>
      {children}
    </section>
  );
}

/** 不可用时统一显示原因；可用时交给 render。 */
function Section<T>({ data, render }: { data: Avail<T>; render: (v: T) => React.ReactNode }) {
  if (data.state === "unavailable") {
    return <div className="cx-unavailable" role="status"><b>未接入</b><span>{data.reason}</span></div>;
  }
  return <>{render(data.value)}</>;
}

function Spark({ points, label, tone = "up" }: { points: number[]; label: string; tone?: "up" | "flat" }) {
  if (points.length < 2) return null;
  const lo = Math.min(...points);
  const hi = Math.max(...points);
  const span = hi - lo || 1;
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${((i / (points.length - 1)) * 100).toFixed(2)},${(36 - ((p - lo) / span) * 32).toFixed(2)}`)
    .join(" ");
  return (
    <svg className={`cx-spark cx-spark-${tone}`} viewBox="0 0 100 40" preserveAspectRatio="none" role="img" aria-label={label}>
      <path className="cx-spark-fill" d={`${d} L100,40 L0,40 Z`} />
      <path className="cx-spark-line" d={d} />
    </svg>
  );
}

function Signed({ value, pct }: { value: string; pct?: string }) {
  const up = !value.startsWith("-");
  return (
    <span className={up ? "cx-up" : "cx-down"}>
      {up ? "▲" : "▼"} {value}{pct ? ` (${pct}%)` : ""}
    </span>
  );
}

function Donut({ slices }: { slices: Slice[] }) {
  let offset = 0;
  return (
    <div className="cx-donut-wrap">
      <svg className="cx-donut" viewBox="0 0 42 42" role="img" aria-label="持仓分布">
        {slices.map((s) => {
          const pct = Number(s.pct);
          const el = (
            <circle key={s.label} className={`cx-slice cx-slice-${s.tone}`} cx="21" cy="21" r="15.9"
              strokeDasharray={`${pct} ${100 - pct}`} strokeDashoffset={25 - offset} />
          );
          offset += pct;
          return el;
        })}
      </svg>
      <ul className="cx-legend">
        {slices.map((s) => (
          <li key={s.label}><i className={`cx-dot cx-slice-${s.tone}`} />{s.label}<b>{s.pct}%</b></li>
        ))}
      </ul>
    </div>
  );
}

function DecisionView({ data }: { data: ConsoleData }) {
  return (
    <div className="cx-decision" data-testid="view-decision">
      <div className="cx-core" aria-hidden="true">
        <div className="cx-ring cx-ring-1" /><div className="cx-ring cx-ring-2" /><div className="cx-ring cx-ring-3" />
        <div className="cx-core-label">AI</div>
      </div>
      <div className="cx-orbit">
        <Section data={data.ai} render={(ai) => (
          <>
            <div className="cx-orbit-card"><h3>主模型</h3><p>{ai.enabled ? ai.model : "未启用"}</p></div>
            <div className="cx-orbit-card"><h3>当前任务</h3><p>{ai.enabled ? ai.task : "首期没有 AI：只运行规则策略"}</p></div>
          </>
        )} />
        <div className="cx-orbit-card"><h3>决策模式</h3><p>{data.mode === "PAPER" ? "模拟盘（PAPER）" : data.mode}</p>
          <small>只读：由后端授权，界面不能切换</small></div>
        <Section data={data.risk} render={(r) => (
          <div className="cx-orbit-card"><h3>风险引擎</h3><p>{r.engine === "OK" ? "正常" : r.engine}</p>
            <small>敞口 {r.exposurePct}% / 上限 {r.exposureLimitPct}%</small></div>
        )} />
      </div>
    </div>
  );
}

function IntelView({ events }: { events: Avail<IntelEvent[]> }) {
  const [selected, setSelected] = useState<string | null>(null);
  if (events.state === "unavailable") {
    return <div className="cx-intel"><div className="cx-unavailable" role="status"><b>情报未接入</b><span>{events.reason}</span></div></div>;
  }
  const list = [...events.value].sort((a, b) => b.at.localeCompare(a.at));
  const chosen = list.find((e) => e.id === selected) ?? null;
  const unlocated = list.filter((e) => e.location === null);
  return (
    <div className="cx-intel" data-testid="view-intel">
      <aside className="cx-feed" aria-label="实时事件流">
        <h3>突发事件</h3>
        <ol>
          {list.map((e) => (
            <li key={e.id}>
              <button type="button" className={`cx-feed-item${e.id === selected ? " is-active" : ""}`} onClick={() => setSelected(e.id)}>
                <time>{hhmm(e.at)}</time>
                <span className="cx-feed-title">{e.title}</span>
                <span className={`cx-sev cx-sev-${e.severity.toLowerCase()}`}>{SEVERITY_TEXT[e.severity]}</span>
              </button>
            </li>
          ))}
        </ol>
      </aside>
      <div className="cx-map">
        <WorldMap events={list} selected={selected} onSelect={setSelected} />
        <ul className="cx-map-legend" aria-label="图例">
          <li><i className="cx-dot hot-critical" />重大</li>
          <li><i className="cx-dot hot-high" />重要</li>
          <li><i className="cx-dot hot-medium" />中</li>
          <li><i className="cx-dot hot-low" />低</li>
        </ul>
        {chosen ? (
          <div className="cx-detail" role="region" aria-label="事件详情" data-testid="intel-detail">
            <h4>{chosen.region} · {chosen.title}</h4>
            <p>来源 {chosen.source} · 类别 {chosen.category} · 关联 {chosen.assets.join("、") || "无"}</p>
            {chosen.location ? <p>定位依据：{chosen.location.basis}（置信度 {Math.round(chosen.location.confidence * 100)}%）</p> : null}
            {chosen.modelView ? <p className="cx-model-view"><b>模型观点</b>（非事实，未经证实）：{chosen.modelView}</p> : null}
            <button type="button" className="cx-link" onClick={() => setSelected(null)}>关闭</button>
          </div>
        ) : null}
        {unlocated.length > 0 ? (
          <p className="cx-unlocated">未定位事件 {unlocated.length} 条（不在地图上标注）</p>
        ) : null}
      </div>
    </div>
  );
}

/** 顶部徽章以最严重的状态为准：HALT 优先，其次未知；风控快照不可用也算未知（规范 AC-07）。 */
function topMode(data: ConsoleData): ConsoleData["mode"] {
  if (data.mode === "HALT" || (data.risk.state === "ok" && data.risk.value.halted)) return "HALT";
  if (data.mode === "UNKNOWN" || data.risk.state === "unavailable") return "UNKNOWN";
  return data.mode;
}

export function Console({ data, clock }: { data: ConsoleData; clock: string }) {
  const [view, setView] = useState<"decision" | "intel">("decision");
  const [still, setStill] = useState(false);
  const mode = topMode(data);
  return (
    <div className={still ? "cx cx-still" : "cx"} data-testid="console">
      {data.demo ? (
        <div className="cx-demo-bar" role="status" data-testid="demo-banner">
          DEMO / SIMULATED · 演示模式：全部为样例数据，不连接账本与券商，不能下单
        </div>
      ) : null}
      <header className="cx-top">
        <div className="cx-brand"><b>AI 量化自动交易系统 · 指挥中枢</b><span>AI Quant Trading Control Center</span></div>
        <div className="cx-top-right">
          <span className={`cx-mode cx-mode-${data.demo && mode === "PAPER" ? "demo" : mode.toLowerCase()}`}
            data-testid="cx-mode">
            {data.demo ? "DEMO · " : ""}{mode === "UNKNOWN" ? "模式未知" : mode}
          </span>
          <time className="cx-clock">{clock} 美东</time>
          <button type="button" className="cx-motion" aria-pressed={still} data-testid="motion-toggle"
            onClick={() => setStill((s) => !s)}>{still ? "动效：已关闭" : "减少动效"}</button>
        </div>
      </header>
      <nav className="cx-nav" aria-label="主导航">
        {NAV.map((n) => (
          <button key={n} type="button" className={n === "AI 指挥中枢" ? "is-active" : ""}
            disabled={n !== "AI 指挥中枢"} title={n === "AI 指挥中枢" ? undefined : "规划中"}>{n}</button>
        ))}
      </nav>

      <main className="cx-grid">
        <Panel title="市场概览" en="Market Overview" source={data.market} testId="panel-market">
          <Section data={data.market} render={(quotes) => (
            <ul className="cx-quotes">
              {quotes.map((q) => (
                <li key={q.symbol}>
                  <div><b>{q.name}</b><span className="cx-meta">{q.symbol}{q.delayed ? " · 延迟" : ""}</span></div>
                  <div className="cx-big">{q.last}</div>
                  <Signed value={q.change} pct={q.changePct} />
                  <Spark points={q.series} label={`${q.name} 走势`} />
                </li>
              ))}
            </ul>
          )} />
        </Panel>

        <section className="cx-center" aria-label="AI 指挥中枢">
          <div className="cx-switch" role="tablist" aria-label="中枢视图">
            <button type="button" role="tab" aria-selected={view === "decision"} data-testid="tab-decision"
              className={view === "decision" ? "is-active" : ""} onClick={() => setView("decision")}>决策分析视图</button>
            <button type="button" role="tab" aria-selected={view === "intel"} data-testid="tab-intel"
              className={view === "intel" ? "is-active" : ""} onClick={() => setView("intel")}>全球情报视图</button>
          </div>
          <div className="cx-view" key={view}>
            {view === "decision" ? <DecisionView data={data} /> : <IntelView events={data.intel} />}
          </div>
        </section>

        <Panel title="账户总览" en="Account Overview" source={data.account} testId="panel-account">
          <Section data={data.account} render={(a) => (
            <>
              <p className="cx-meta">IBKR 模拟账户 {a.accountMasked} · {a.currency}</p>
              <div className="cx-big cx-nav-value">${a.nav}</div>
              <Signed value={a.dayPnl} />
              <dl className="cx-kv">
                <div><dt>已结算现金</dt><dd>${a.settledCash}</dd></div>
                <div><dt>持仓市值</dt><dd>${a.positionsValue}</dd></div>
              </dl>
              <Spark points={a.curve} label="账户净值曲线" />
            </>
          )} />
          <h3 className="cx-sub">持仓分布</h3>
          <Section data={data.allocation} render={(s) => <Donut slices={s} />} />
        </Panel>

        <Panel title="决策与交易动态" en="Activity Feed" source={data.activity} testId="panel-activity">
          <Section data={data.activity} render={(items) => (
            <ol className="cx-activity">
              {items.map((i) => (
                <li key={`${i.at}-${i.text}`}>
                  <time>{hhmm(i.at)}</time>
                  <span className={`cx-kind cx-kind-${i.kind.toLowerCase()}`}>{KIND_TEXT[i.kind]}</span>
                  <span>{i.text}</span>
                </li>
              ))}
            </ol>
          )} />
        </Panel>

        <Panel title="策略表现" en="Strategy Performance" source={data.strategy} testId="panel-strategy">
          <Section data={data.strategy} render={(s) => (
            <>
              <p className="cx-meta">{s.window} · {s.basis === "PAPER" ? "模拟盘" : "回测"} · 样本 {s.samples} 天 · 已扣成本</p>
              <dl className="cx-kv cx-kv-4">
                <div><dt>收益率</dt><dd>{s.returnPct === null ? "N/A" : `${s.returnPct}%`}</dd></div>
                <div><dt>胜率</dt><dd>{s.winRate === null ? "N/A" : `${s.winRate}%`}</dd></div>
                <div><dt>夏普比率</dt><dd>{s.sharpe ?? "N/A"}</dd></div>
                <div><dt>最大回撤</dt><dd>{s.maxDrawdown === null ? "N/A" : `${s.maxDrawdown}%`}</dd></div>
              </dl>
              <Spark points={s.curve} label="策略净值" />
            </>
          )} />
        </Panel>

        <Panel title="风险控制" en="Risk Control" source={data.risk} testId="panel-risk">
          <Section data={data.risk} render={(r) => (
            <>
              <p className={r.halted ? "cx-risk cx-risk-halt" : "cx-risk"}>
                {r.halted ? "⛔ HALT：停止新增风险" : "✓ 风控状态：正常"}
              </p>
              <dl className="cx-kv">
                <div><dt>当前敞口</dt><dd>{r.exposurePct}%</dd></div>
                <div><dt>敞口上限</dt><dd>{r.exposureLimitPct}%</dd></div>
                <div><dt>已结算现金</dt><dd>${r.settledCash}</dd></div>
                <div><dt>未处理告警</dt><dd>{r.alerts}</dd></div>
              </dl>
              <div className="cx-bar" role="meter" aria-label="敞口使用率" aria-valuenow={Number(r.exposurePct)}
                aria-valuemin={0} aria-valuemax={Number(r.exposureLimitPct)}>
                <i style={{ width: `${Math.min(100, (Number(r.exposurePct) / Number(r.exposureLimitPct)) * 100)}%` }} />
              </div>
              <small className="cx-meta">数值来自独立风险引擎，只读；调整上限在风控面板，调松要二次确认</small>
            </>
          )} />
        </Panel>

        <Panel title="系统运行状态" en="System Status" source={data.health} testId="panel-health">
          <Section data={data.health} render={(items) => (
            <ul className="cx-health">
              {items.map((h) => (
                <li key={h.name}>
                  <span>{h.name}</span>
                  <span className={`cx-state cx-state-${h.state.toLowerCase()}`}>
                    {h.state === "OK" ? "●" : "▲"} {HEALTH_TEXT[h.state]}
                  </span>
                </li>
              ))}
            </ul>
          )} />
        </Panel>
      </main>
    </div>
  );
}
