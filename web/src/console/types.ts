// 指挥中枢的数据契约（UI-02，对应《科幻操作面板 UI 规范 V1.0》第 7 节）。
//
// 每个区块要么有数据（附数据时间与来源），要么明确“不可用”并说明原因。
// 面板只渲染这里给的东西：不自己生成数字，不拿旧值冒充新值。
// 真实模式下，没有后端接口的区块一律是 unavailable（“未接入”）；样例只在 DEMO 模式出现。

export type Avail<T> =
  | { state: "ok"; value: T; asOf: string; source: string }
  | { state: "unavailable"; reason: string };

export type ModeView = "PAPER" | "HALT" | "UNKNOWN";

export interface IndexQuote {
  symbol: string; // 例如 "SPY"（用 ETF 代替指数时写明口径）
  name: string;
  last: string; // 价格一律是后端给的十进制字符串，界面不做浮点运算后再显示
  change: string; // 带正负号
  changePct: string;
  delayed: boolean;
  series: number[]; // 只用于画迷你图的形状，不显示其中的数字
}

export interface AccountView {
  accountMasked: string; // 只显示脱敏后的账户，例如 "DU••••567"
  currency: "USD";
  nav: string;
  settledCash: string;
  positionsValue: string;
  dayPnl: string;
  curve: number[];
}

export interface Slice {
  label: string;
  pct: string; // 例如 "52.3"
  tone: "a" | "b" | "c" | "d";
}

export type ActivityKind = "SUGGESTION" | "RISK_PASSED" | "SUBMITTED" | "FILLED" | "DATA" | "MANUAL";

export interface ActivityItem {
  at: string; // ISO 时间（UTC）
  kind: ActivityKind;
  text: string;
  ref: string | null; // 意图 ID、订单号等，可追溯
}

export interface StrategyPerf {
  window: string;
  basis: "PAPER" | "BACKTEST";
  returnPct: string | null; // 样本不足时为 null，显示 N/A
  winRate: string | null;
  sharpe: string | null;
  maxDrawdown: string | null;
  samples: number;
  curve: number[];
}

export interface RiskView {
  engine: "OK" | "DEGRADED" | "DOWN";
  halted: boolean;
  exposurePct: string;
  exposureLimitPct: string;
  settledCash: string;
  alerts: number;
}

export type Health = "OK" | "WARN" | "DEGRADED" | "DOWN" | "UNKNOWN";

export interface HealthItem {
  name: string;
  state: Health;
  lastBeat: string | null;
}

export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";

export interface IntelEvent {
  id: string;
  at: string;
  region: string;
  title: string;
  severity: Severity;
  category: string;
  source: string;
  // 定位依据与置信度：没有可靠定位时为 null，进入“未定位事件”列表，不在地图上乱放
  location: { lon: number; lat: number; confidence: number; basis: string } | null;
  assets: string[];
  modelView: string | null; // 模型观点（必须标明，不能画成已证实的因果）
}

export interface AiView {
  enabled: boolean; // 首期没有 AI：false 时卡片显示“未启用”
  model: string | null;
  task: string | null;
}

export interface ConsoleData {
  demo: boolean; // true：全局显示 DEMO / SIMULATED，不请求任何交易接口
  mode: ModeView;
  market: Avail<IndexQuote[]>;
  account: Avail<AccountView>;
  allocation: Avail<Slice[]>;
  activity: Avail<ActivityItem[]>;
  strategy: Avail<StrategyPerf>;
  risk: Avail<RiskView>;
  health: Avail<HealthItem[]>;
  intel: Avail<IntelEvent[]>;
  ai: Avail<AiView>;
}
