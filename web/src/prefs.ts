// 界面偏好（S-13）：主题；以后加语言（S-12）。存在 localStorage 的 qts.prefs 里，
// 只放界面偏好，不放令牌或任何交易数据（令牌只在 sessionStorage）。
import { useEffect, useState } from "react";

export type Theme = "system" | "dark" | "light";
const KEY = "qts.prefs";

interface Prefs {
  theme: Theme;
}

function load(): Prefs {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Partial<Prefs>;
    const theme = raw.theme === "dark" || raw.theme === "light" || raw.theme === "system" ? raw.theme : "dark";
    return { theme };
  } catch {
    return { theme: "dark" }; // 默认深色
  }
}

function save(prefs: Prefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // 存储不可用：本次会话有效
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => load().theme);
  useEffect(() => {
    applyTheme(theme);
    save({ ...load(), theme });
  }, [theme]);
  return [theme, setTheme];
}
