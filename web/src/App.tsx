// 客户端外壳（S-04 / S-13）：令牌登录、顶部模式标识与连接提示、页面切换、主题。
// #showroom 打开组件陈列室（只有静态示例，不需要登录，不请求接口）。
import { useCallback, useEffect, useState } from "react";
import { call, getToken, setToken } from "./api/client";
import type { WhoAmI } from "./api/types.gen";
import { CommandStore } from "./commands";
import { Alerts, Audit } from "./pages/Alerts";
import { Diagnostics } from "./pages/Diagnostics";
import { Emergency } from "./pages/Emergency";
import { Showroom } from "./pages/Showroom";
import { Orders, Overview, Positions, Risk, connectionText } from "./pages/Views";
import { LangProvider, type MessageKey, useT } from "./i18n";
import { type Theme, useLangPref, useTheme } from "./prefs";
import { modeLabel, useStatus } from "./status";
import { Button, Icon, type IconName, LangToggle, ModeBadge, Notice, Tag, ThemeToggle } from "./ui";
import type { Lang } from "./i18n";

const PAGES: readonly [string, MessageKey, IconName][] = [
  ["overview", "nav.overview", "overview"],
  ["orders", "nav.orders", "orders"],
  ["positions", "nav.positions", "positions"],
  ["risk", "nav.risk", "risk"],
  ["alerts", "nav.alerts", "bell"],
  ["audit", "nav.audit", "log"],
  ["diagnostics", "nav.diagnostics", "tool"],
  ["emergency", "nav.emergency", "stop"],
];

interface Prefs {
  theme: Theme;
  setTheme: (t: Theme) => void;
  lang: Lang;
  setLang: (l: Lang) => void;
}

function Login({ onLogin, prefs }: { onLogin: () => void; prefs: Prefs }) {
  const t = useT();
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
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>QTS</h1>
        <LangToggle lang={prefs.lang} onChange={prefs.setLang} />
      </div>
      <p className="hint">{t("app.tagline")}</p>
      <label className="field">
        {t("login.token")}
        <input className="input" type="password" autoComplete="off" value={token} autoFocus
          onChange={(e) => setValue(e.target.value)} data-testid="token-input" />
      </label>
      <Button kind="primary" type="submit" disabled={token.trim() === ""} data-testid="login">{t("login.submit")}</Button>
      {error ? (
        <div style={{ marginTop: "var(--s3)" }}>
          <Notice tone="danger" title={t("login.failed")} testId="login-error">{t("login.failedHint", { error })}</Notice>
        </div>
      ) : null}
    </form>
  );
}

function Shell({ onLogout, prefs }: { onLogout: () => void; prefs: Prefs }) {
  const t = useT();
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
        <Tag tone={connTone}><Icon name="wifi" size={14} /><span data-testid="connection">{t(connectionText(state))}</span></Tag>
        <span className="spacer" />
        <span className="who" data-testid="who">
          {who ? `${who.name} · ${who.role}` : whoError ? t("top.whoUnconfirmed", { error: whoError }) : ""}
        </span>
        <LangToggle lang={prefs.lang} onChange={prefs.setLang} />
        <ThemeToggle theme={prefs.theme} onChange={prefs.setTheme} />
        <Button kind="ghost" small icon="logout" onClick={() => { setToken(null); onLogout(); }}>{t("top.logout")}</Button>
      </header>
      {mode === "HALT" ? (
        <div className="halt-band" role="alert">
          <Icon name="stop" />{t("banner.halt", { reason: state.status?.halt?.reason ?? "" })}
        </div>
      ) : null}
      {state.connection === "ok" && mode === "UNKNOWN" ? (
        <div className="stale" role="alert" data-testid="untrusted-banner">
          <Icon name="alert" />{t("banner.untrusted")}
        </div>
      ) : null}
      {state.connection !== "ok" ? (
        <div className="stale" role="alert" data-testid="stale-banner">
          <Icon name="clock" />{t("banner.stale")}{state.error ? ` · ${state.error}` : ""}
        </div>
      ) : null}
      <nav className="tabs" aria-label={t("nav.label")}>
        {PAGES.map(([id, title, icon]) => (
          <button type="button" key={id} className={page === id ? "active" : ""} aria-current={page === id ? "page" : undefined}
            onClick={() => setPage(id)} data-testid={`nav-${id}`}>
            <Icon name={icon} size={16} />{t(title)}
          </button>
        ))}
      </nav>
      <main>
        {page === "overview" ? <Overview state={state} /> : null}
        {page === "orders" ? <Orders /> : null}
        {page === "positions" ? <Positions /> : null}
        {page === "risk" ? <Risk state={state} /> : null}
        {page === "alerts" ? <Alerts canAck={who?.role === "owner"} /> : null}
        {page === "audit" ? <Audit /> : null}
        {page === "diagnostics" ? <Diagnostics canUse={who?.role === "owner"} /> : null}
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
  const [theme, setTheme] = useTheme(); // 主题与语言只在这里持有：登录页、陈列室、外壳共用
  const [lang, setLang] = useLangPref();
  const prefs: Prefs = { theme, setTheme, lang, setLang };
  // CommandStore 在最外层：退出、令牌失效、打开陈列室再回来，结果未知的编号都还在（只在内存里）
  // 陈列室只在开发与测试构建里提供（正式构建不含示例页面）
  const showroom = import.meta.env.DEV && hash === "#showroom";
  return (
    <LangProvider lang={lang}>
      <CommandStore>
        {showroom ? (
          <Showroom theme={theme} setTheme={setTheme} />
        ) : loggedIn ? (
          <Shell onLogout={logout} prefs={prefs} />
        ) : (
          <Login onLogin={() => setLoggedIn(true)} prefs={prefs} />
        )}
      </CommandStore>
    </LangProvider>
  );
}
