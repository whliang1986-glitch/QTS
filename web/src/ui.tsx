// 通用界面组件（S-13）。颜色、间距只来自 styles.css 的变量；状态除了颜色都带文字或符号。
import type { ReactNode } from "react";
import type { Theme } from "./prefs";

// ---------- 线性图标（24 网格，currentColor，1.75 线宽） ----------

const PATHS = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  orders: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  positions: "M3 7l9-4 9 4-9 4-9-4Zm0 5 9 4 9-4M3 17l9 4 9-4",
  risk: "M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6l-8-3Z",
  stop: "M8.5 3h7L21 8.5v7L15.5 21h-7L3 15.5v-7L8.5 3ZM12 8v5m0 3h.01",
  check: "M5 12.5 10 17 19 7",
  alert: "M12 4 2.5 20h19L12 4Zm0 6v4m0 3h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-11v6m0-9h.01",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v4l3 2",
  wifi: "M2 8.5a15 15 0 0 1 20 0M5 12a10 10 0 0 1 14 0M8.5 15.5a5 5 0 0 1 7 0M12 19h.01",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Zm0-15v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z",
  monitor: "M3 5h18v11H3V5Zm6 15h6m-3-4v4",
  logout: "M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10",
  inbox: "M3 13h5l2 3h4l2-3h5M5 5h14l2 8v6H3v-6l2-8Z",
  chart: "M3 20h18M5 16l4-5 4 3 6-8",
} as const;
export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, label }: { name: IconName; size?: number; label?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
      <path d={PATHS[name]} />
    </svg>
  );
}

// ---------- 模式标识（三态，颜色之外必有文字） ----------

export type ModeText = "PAPER" | "HALT" | "未知";

export function ModeBadge({ mode, sample }: { mode: ModeText; sample?: boolean }) {
  const kind = mode === "HALT" ? "halt" : mode === "未知" ? "unknown" : "paper";
  const icon: IconName = mode === "HALT" ? "stop" : mode === "未知" ? "alert" : "check";
  const note = mode === "PAPER" ? "模拟盘" : mode === "HALT" ? "已停止新增风险" : "状态不可信";
  return (
    <span className={`mode ${kind}`} role={sample ? undefined : "status"}
      aria-label={`${sample ? "示例" : "当前模式"}：${mode}，${note}`}>
      <Icon name={icon} size={16} />
      <span data-testid={sample ? undefined : "mode-badge"}>{mode}</span>
      <span className="mode-note">{note}</span>
    </span>
  );
}

// ---------- 标签 ----------

export type Tone = "ok" | "warn" | "danger" | "info" | "neutral";

export function Tag({ tone = "neutral", children, testId }: {
  tone?: Tone;
  children: ReactNode;
  testId?: string;
}) {
  return <span className={`tag ${tone}`} data-testid={testId}>{children}</span>;
}

/** 订单状态 → 标签语气。 */
export function statusTone(status: string | null): Tone {
  switch (status) {
    case "FILLED":
      return "ok";
    case "SUBMITTED":
    case "PARTIALLY_FILLED":
    case "RISK_APPROVED":
    case "SUBMITTING":
      return "info";
    case "UNKNOWN":
    case "CANCEL_PENDING":
      return "warn";
    case "REJECTED":
    case "REJECTED_BY_RISK":
      return "danger";
    default:
      return "neutral";
  }
}

// ---------- 按钮 ----------

export type ButtonKind = "default" | "primary" | "ghost" | "danger-outline" | "danger";

export function Button({ kind = "default", small, icon, children, ...rest }: {
  kind?: ButtonKind;
  small?: boolean;
  icon?: IconName;
  children: ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const cls = ["btn", kind === "default" ? "" : kind, small ? "small" : ""].filter(Boolean).join(" ");
  return (
    <button type="button" className={cls} {...rest}>
      {icon ? <Icon name={icon} size={16} /> : null}
      {children}
    </button>
  );
}

// ---------- 数字卡片 ----------

export function StatCard({ label, icon, value, sub, testId }: {
  label: string;
  icon?: IconName;
  value: string | null; // null：暂无数据（骨架屏）
  sub?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="card">
      <div className="stat-label">{icon ? <Icon name={icon} size={16} /> : null}{label}</div>
      {value === null ? (
        <>
          <span className="skeleton" aria-hidden />
          <div className="stat-sub hint" data-testid={testId}>暂无数据</div>
        </>
      ) : (
        <>
          <div className="stat-value" data-testid={testId}>{value}</div>
          {sub ? <div className="stat-sub">{sub}</div> : null}
        </>
      )}
    </div>
  );
}

/** 盈亏：颜色 + 符号 + 箭头，三重信号（色弱友好）。value 是十进制字符串。 */
export function PnL({ value }: { value: string }) {
  const text = value.trim();
  if (!/^[+-]?\d+(\.\d+)?$/.test(text)) return <span className="hint">暂无数据</span>; // 空值或非法值不显示成 0
  const negative = text.startsWith("-");
  if (/^[+-]?0+(\.0+)?$/.test(text)) return <span className="num">0.00</span>;
  const abs = text.replace(/^[+-]/, "");
  return (
    <span className={`num ${negative ? "down" : "up"}`}>
      {negative ? "▼ −" : "▲ +"}{abs}
    </span>
  );
}

// ---------- 提示条 ----------

const NOTICE_ICON: Record<Exclude<Tone, "neutral">, IconName> = {
  ok: "check", warn: "alert", danger: "alert", info: "info",
};

export function Notice({ tone, title, children, action, testId }: {
  tone: Exclude<Tone, "neutral">;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  testId?: string;
}) {
  return (
    <div className={`notice ${tone}`} role={tone === "danger" || tone === "warn" ? "alert" : "status"} data-testid={testId}>
      <span className={`icon-${tone}`}><Icon name={NOTICE_ICON[tone]} /></span>
      <div className="notice-body">
        <div className="notice-title">{title}</div>
        {children ? <div className="muted">{children}</div> : null}
        {action ? <div className="row" style={{ marginTop: "var(--s2)" }}>{action}</div> : null}
      </div>
    </div>
  );
}

// ---------- 空状态 ----------

export function Empty({ icon = "inbox", title, children, testId }: {
  icon?: IconName;
  title: string;
  children?: ReactNode;
  testId?: string;
}) {
  return (
    <div className="empty" data-testid={testId}>
      <Icon name={icon} size={40} />
      <div className="muted">{title}</div>
      {children ? <div className="hint">{children}</div> : null}
    </div>
  );
}

// ---------- 主题切换 ----------

const THEMES: { id: Theme; icon: IconName; label: string }[] = [
  { id: "dark", icon: "moon", label: "深色" },
  { id: "light", icon: "sun", label: "浅色" },
  { id: "system", icon: "monitor", label: "跟随系统" },
];

export function ThemeToggle({ theme, onChange }: { theme: Theme; onChange: (t: Theme) => void }) {
  return (
    <div className="row" role="radiogroup" aria-label="主题" style={{ gap: "var(--s1)" }}>
      {THEMES.map((t) => (
        <button key={t.id} type="button" role="radio" aria-checked={theme === t.id}
          className={`btn small ${theme === t.id ? "" : "ghost"}`} title={t.label}
          onClick={() => onChange(t.id)} data-testid={`theme-${t.id}`}>
          <Icon name={t.icon} size={16} label={t.label} />
        </button>
      ))}
    </div>
  );
}

// ---------- 净值曲线（无依赖的简洁 SVG） ----------

export function Sparkline({ points, label }: { points: number[]; label: string }) {
  if (points.length < 2) {
    return <Empty icon="chart" title="暂无净值数据" testId="nav-curve-empty">等待 tradingd 发布账户快照</Empty>;
  }
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const d = points.map((p, i) => `${(i / (points.length - 1)) * 100},${36 - ((p - min) / span) * 32 - 2}`).join(" ");
  return (
    <svg viewBox="0 0 100 36" preserveAspectRatio="none" width="100%" height="120" role="img" aria-label={label}>
      <polyline points={d} fill="none" stroke="var(--brand)" strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
