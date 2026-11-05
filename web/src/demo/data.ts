// DEMO 样例数据（UI-02）：只给演示模式用，生产构建里没有这个模块（tests/arch/test_web_demo_isolation.py 检查）。
//
// - 全部是写死的样例，不用随机数，每次打开都一样（截图与视觉回归可复现）；
// - 数字、新闻、账户都是虚构的，只为展示布局，不代表任何真实行情或账户；
// - 账户号是虚构的，并且已脱敏；
// - 首期范围：持仓只有股票、ETF、现金，没有期权与保证金；AI 卡片显示“未启用”。
import type { ConsoleData } from "../console/types";

const AS_OF = "2026-11-05T19:45:32Z"; // 美东 14:45:32
const SRC = "DEMO 样例";

function wave(start: number, step: number, n: number, wiggle: number): number[] {
  // 确定性的“走势”：线性趋势加固定的锯齿，只用于画迷你图的形状
  return Array.from({ length: n }, (_, i) => start + step * i + (i % 3 === 0 ? wiggle : i % 3 === 1 ? -wiggle / 2 : 0));
}

export function demoData(): ConsoleData {
  return {
    demo: true,
    mode: "PAPER",
    market: {
      state: "ok", asOf: AS_OF, source: `${SRC}（用 ETF 代替指数）`,
      value: [
        { symbol: "SPY", name: "标普 500", last: "664.01", change: "+3.21", changePct: "+0.48", delayed: false, series: wave(650, 0.6, 24, 1.8) },
        { symbol: "QQQ", name: "纳斯达克 100", last: "602.18", change: "+3.05", changePct: "+0.51", delayed: false, series: wave(585, 0.7, 24, 2.2) },
        { symbol: "DIA", name: "道琼斯", last: "467.06", change: "+3.16", changePct: "+0.68", delayed: true, series: wave(458, 0.4, 24, 1.2) },
      ],
    },
    account: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: {
        accountMasked: "DU••••000", currency: "USD", nav: "248,750.32", settledCash: "142,580.21",
        positionsValue: "106,170.11", dayPnl: "+2,842.17", curve: wave(240000, 340, 30, 900),
      },
    },
    allocation: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: [
        { label: "ETF", pct: "38.6", tone: "a" },
        { label: "股票", pct: "4.1", tone: "b" },
        { label: "现金（已结算）", pct: "57.3", tone: "d" },
      ],
    },
    activity: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: [
        { at: "2026-11-05T19:44:12Z", kind: "SUGGESTION", text: "基准策略：月度轮动建议买入 SPY 12 股", ref: "demo-intent-1" },
        { at: "2026-11-05T19:43:58Z", kind: "RISK_PASSED", text: "风控通过：敞口 12.3%，未超上限", ref: "demo-intent-1" },
        { at: "2026-11-05T19:43:30Z", kind: "SUBMITTED", text: "已提交券商：限价买入 SPY 12 股 @ 663.90", ref: "demo-order-1" },
        { at: "2026-11-05T19:42:30Z", kind: "FILLED", text: "已成交：SPY 12 股 @ 663.88", ref: "demo-exec-1" },
        { at: "2026-11-05T19:41:20Z", kind: "DATA", text: "数据更新：日线与报价通过质量门禁", ref: null },
      ],
    },
    strategy: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: {
        window: "近一月", basis: "PAPER", returnPct: "+1.32", winRate: null, sharpe: null, maxDrawdown: "-1.12",
        samples: 21, curve: wave(100, 0.06, 21, 0.25),
      },
    },
    risk: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: { engine: "OK", halted: false, exposurePct: "12.3", exposureLimitPct: "70.0", settledCash: "142,580.21", alerts: 0 },
    },
    health: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: [
        { name: "交易核心 tradingd", state: "OK", lastBeat: AS_OF },
        { name: "IBKR 连接", state: "OK", lastBeat: AS_OF },
        { name: "数据库", state: "OK", lastBeat: AS_OF },
        { name: "策略引擎", state: "OK", lastBeat: AS_OF },
        { name: "行情", state: "WARN", lastBeat: AS_OF },
        { name: "AI 模型服务", state: "UNKNOWN", lastBeat: null },
      ],
    },
    intel: {
      state: "ok", asOf: AS_OF, source: SRC,
      value: [
        { id: "e1", at: "2026-11-05T19:45:00Z", region: "美国 · 纽约", title: "科技股午后反弹，纳指涨超 1%", severity: "HIGH", category: "市场异动", source: "DEMO 新闻源", location: { lon: -74.0, lat: 40.7, confidence: 0.9, basis: "交易所所在地" }, assets: ["QQQ", "XLK"], modelView: null },
        { id: "e2", at: "2026-11-05T19:43:00Z", region: "欧盟 · 布鲁塞尔", title: "欧盟发布新一轮 AI 监管框架", severity: "HIGH", category: "政策动态", source: "DEMO 新闻源", location: { lon: 4.35, lat: 50.85, confidence: 0.85, basis: "发布机构所在地" }, assets: ["SOXX"], modelView: "可能影响大型科技股的合规成本（相关性推测，未经证实）" },
        { id: "e3", at: "2026-11-05T19:40:00Z", region: "美国 · 华盛顿", title: "美联储维持利率不变", severity: "CRITICAL", category: "央行", source: "DEMO 新闻源", location: { lon: -77.04, lat: 38.9, confidence: 0.95, basis: "发布机构所在地" }, assets: ["IEF", "TLT"], modelView: null },
        { id: "e4", at: "2026-11-05T19:38:00Z", region: "中东", title: "地区局势再度紧张，油价短线拉升", severity: "MEDIUM", category: "能源", source: "DEMO 新闻源", location: { lon: 51.4, lat: 25.3, confidence: 0.5, basis: "报道提及的地区（粗略）" }, assets: ["XLE"], modelView: null },
        { id: "e5", at: "2026-11-05T19:35:00Z", region: "东亚", title: "半导体供应链传出调整消息", severity: "LOW", category: "供应链", source: "DEMO 新闻源", location: { lon: 121.5, lat: 25.0, confidence: 0.6, basis: "报道提及的城市" }, assets: ["SOXX"], modelView: null },
        { id: "e6", at: "2026-11-05T19:30:00Z", region: "未定位", title: "社交媒体热议某消费品牌", severity: "LOW", category: "企业公告", source: "DEMO 社媒源", location: null, assets: [], modelView: null },
      ],
    },
    ai: { state: "ok", asOf: AS_OF, source: SRC, value: { enabled: false, model: null, task: null } },
  };
}

export function demoClock(): string {
  return "2026-11-05 14:45:32";
}
