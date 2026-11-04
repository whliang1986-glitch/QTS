// 客户端外壳（S-04 / S-13）：令牌登录、顶部模式标识与连接提示、页面切换、主题。
// #showroom 打开组件陈列室（只有静态示例，不需要登录，不请求接口）。
import { useCallback, useEffect, useState } from "react";
import { call, getToken, setToken } from "./api/client";
import type { WhoAmI } from "./api/types.gen";
import { CommandStore } from "./commands";
import { Emergency } from "./pages/Emergency";
import { Showroom } from "./pages/Showroom";
import { Orders, Overview, Positions, Risk, connectionText } from "./pages/Views";
import { type Theme, useTheme } from "./prefs";
import { modeLabel, useStatus } from "./status";
import { Button, Icon, type IconName, ModeBadge, Notice, Tag, ThemeToggle } from "./ui";

const PAGES: readonly [string, string, IconName][] = [
  ["overview", "总览", "overview"],
  ["orders", "订单", "orders"],
  ["positions", "持仓", "positions"],
  ["risk", "风险", "risk"],
  ["emergency", "急停", "stop"],
];

function Login({ onLogin }: { onLogin: () => void }) {
  const [token, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setToken(token.trim());
    const who = await call<WhoAmI>("GET", "/v1/whoami");
    if (who.ok) {
      onLogin();
    } else {
      setToken(null);
      setError(who.error);
    }
  };
  return (
    <form className="login card" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <h1>QTS</h1>
      <p className="hint">量化交易控制台 · 令牌只保存在当前标签页</p>
      <label className="field">
        访问令牌
        <input className="input" type="password" autoComplete="off" value={token} autoFocus
          onChange={(e) => setValue(e.target.value)} data-testid="token-input" />
      </label>
      <Button kind="primary" type="submit" disabled={token.trim() === ""} data-testid="login">登录</Button>
      {error ? (
        <div style={{ marginTop: "var(--s3)" }}>
          <Notice tone="danger" title="登录失败" testId="login-error">{error}。令牌错误或过期时请换一个；服务器繁忙时请稍后再试。</Notice>
        </div>
      ) : null}
    </form>
  );
}

function Shell({ onLogout, theme, setTheme }: {
  onLogout: () => void;
  theme: Theme;
  setTheme: (t: Theme) => void;
}) {
  const [page, setPage] = useState("overview");
  const [who, setWho] = useState<WhoAmI | null>(null);
  const [whoError, setWhoError] = useState<string | null>(null);
  const state = useStatus(onLogout);
  useEffect(() => {
    // 身份没确认之前不显示命令控件（失败即关闭）；失败时提示并每 5 秒重试
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      const r = await call<WhoAmI>("GET", "/v1/whoami");
      if (!alive) return;
      if (r.ok) {
        setWho(r.data);
        setWhoError(null);
        return;
      }
      if (r.status === 401) {
        onLogout();
        return;
      }
      setWhoError(r.error);
      timer = window.setTimeout(() => void load(), 5000);
    };
    void load();
    return () => {
      alive = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [onLogout]);
  const mode = modeLabel(state);
  const connTone = state.connection === "ok" ? "ok" : "warn";
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">QTS</span>
        <ModeBadge mode={mode} />
        <Tag tone={connTone}><Icon name="wifi" size={14} /><span data-testid="connection">{connectionText(state)}</span></Tag>
        <span className="spacer" />
        <span className="who" data-testid="who">
          {who ? `${who.name} · ${who.role}` : whoError ? `身份未确认：${whoError}` : ""}
        </span>
        <ThemeToggle theme={theme} onChange={setTheme} />
        <Button kind="ghost" small icon="logout" onClick={() => { setToken(null); onLogout(); }}>退出</Button>
      </header>
      {mode === "HALT" ? (
        <div className="halt-band" role="alert">
          <Icon name="stop" />已停止新增风险（HALT）：{state.status?.halt?.reason ?? ""}。恢复须走恢复流程。
        </div>
      ) : null}
      {state.connection === "ok" && mode === "未知" ? (
        <div className="stale" role="alert" data-testid="untrusted-banner">
          <Icon name="alert" />服务器返回的运行状态不完整或无法识别，界面按“未知”处理。请以券商端为准。
        </div>
      ) : null}
      {state.connection !== "ok" ? (
        <div className="stale" role="alert" data-testid="stale-banner">
          <Icon name="clock" />数据可能已过期，服务器仍在运行。{state.error ? `（${state.error}）` : ""}
        </div>
      ) : null}
      <nav className="tabs" aria-label="页面">
        {PAGES.map(([id, title, icon]) => (
          <button type="button" key={id} className={page === id ? "active" : ""} aria-current={page === id ? "page" : undefined}
            onClick={() => setPage(id)} data-testid={`nav-${id}`}>
            <Icon name={icon} size={16} />{title}
          </button>
        ))}
      </nav>
      <main>
        {page === "overview" ? <Overview state={state} /> : null}
        {page === "orders" ? <Orders /> : null}
        {page === "positions" ? <Positions /> : null}
        {page === "risk" ? <Risk state={state} /> : null}
        {page === "emergency" ? <Emergency canCommand={who?.role === "owner"} /> : null}
      </main>
    </div>
  );
}

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const [loggedIn, setLoggedIn] = useState(getToken() !== null);
  const logout = useCallback(() => setLoggedIn(false), []);
  const hash = useHash();
  const [theme, setTheme] = useTheme(); // 主题只在这里持有：登录页、陈列室、外壳共用
  // CommandStore 在最外层：退出、令牌失效、打开陈列室再回来，结果未知的编号都还在（只在内存里）
  // 陈列室只在开发与测试构建里提供（正式构建不含示例页面）
  const showroom = import.meta.env.DEV && hash === "#showroom";
  return (
    <CommandStore>
      {showroom ? (
        <Showroom theme={theme} setTheme={setTheme} />
      ) : loggedIn ? (
        <Shell onLogout={logout} theme={theme} setTheme={setTheme} />
      ) : (
        <Login onLogin={() => setLoggedIn(true)} />
      )}
    </CommandStore>
  );
}
