// 客户端外壳（S-04）：令牌登录、顶部模式标识与连接提示、页面切换。
import { useCallback, useEffect, useState } from "react";
import { call, getToken, setToken } from "./api/client";
import type { WhoAmI } from "./api/types.gen";
import { CommandStore } from "./commands";
import { Emergency } from "./pages/Emergency";
import { Orders, Overview, Positions, Risk, connectionText } from "./pages/Views";
import { modeLabel, useStatus } from "./status";

const PAGES = [
  ["overview", "总览"],
  ["orders", "订单"],
  ["positions", "持仓"],
  ["risk", "风险"],
  ["emergency", "急停"],
] as const;
type Page = (typeof PAGES)[number][0];

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
    <div className="login">
      <h1>QTS</h1>
      <label>
        访问令牌
        <input type="password" autoComplete="off" value={token}
          onChange={(e) => setValue(e.target.value)} data-testid="token-input" />
      </label>
      <button type="button" onClick={() => void submit()} disabled={token.trim() === ""} data-testid="login">
        登录
      </button>
      {error ? <p className="warn" data-testid="login-error">{error}</p> : null}
    </div>
  );
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const [page, setPage] = useState<Page>("overview");
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
  const modeClass = mode === "HALT" ? "halt" : mode === "未知" ? "unknown" : "paper";
  return (
    <div>
      <header className={`modebar ${modeClass}`}>
        <span className="mode" data-testid="mode-badge">{mode}</span>
        <span data-testid="connection">{connectionText(state)}</span>
        <span data-testid="who">{who ? `${who.name}（${who.role}）` : whoError ? `身份未确认：${whoError}` : ""}</span>
        <button type="button" onClick={() => { setToken(null); onLogout(); }}>退出</button>
      </header>
      {state.connection !== "ok" ? (
        <div className="banner" data-testid="stale-banner">
          数据可能已过期，服务器仍在运行。{state.error ? `（${state.error}）` : ""}
        </div>
      ) : null}
      <nav>
        {PAGES.map(([id, title]) => (
          <button type="button" key={id} className={page === id ? "active" : ""}
            onClick={() => setPage(id)} data-testid={`nav-${id}`}>
            {title}
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

export function App() {
  const [loggedIn, setLoggedIn] = useState(getToken() !== null);
  const logout = useCallback(() => setLoggedIn(false), []);
  // CommandStore 放在登录判断之外：退出或令牌失效后重新登录，结果未知的编号仍在（只在内存里）
  return (
    <CommandStore>
      {loggedIn ? <Shell onLogout={logout} /> : <Login onLogin={() => setLoggedIn(true)} />}
    </CommandStore>
  );
}
