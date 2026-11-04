// 界面偏好（S-13 主题、S-12 语言）。存在 localStorage 的 qts.prefs 里，
// 只放界面偏好，不放令牌或任何交易数据（令牌只在 sessionStorage）。
import { useEffect, useState } from "react";
import { LANGS, type Lang, detectLang } from "./i18n";

export type Theme = "system" | "dark" | "light";
const KEY = "qts.prefs";

interface Prefs {
  theme?: Theme;
  lang?: Lang;
}

function load(): Prefs {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "{}") as Record<string, unknown>;
    const theme = raw.theme === "dark" || raw.theme === "light" || raw.theme === "system" ? raw.theme : undefined;
    const lang = (LANGS as readonly unknown[]).includes(raw.lang) ? (raw.lang as Lang) : undefined;
    return { theme, lang };
  } catch {
    return {};
  }
}

function save(patch: Prefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...load(), ...patch }));
  } catch {
    // 存储不可用：本次会话有效
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => load().theme ?? "dark"); // 默认深色
  useEffect(() => {
    applyTheme(theme);
    save({ theme });
  }, [theme]);
  return [theme, setTheme];
}

// 当前语言（模块级）：给 React 之外的代码用（api/client 的错误文案）
let current: Lang = "zh";
export function currentLang(): Lang {
  return current;
}

/** 语言：用户选过就用选过的；没选过跟随浏览器（认不出用中文），不写入存储。 */
export function useLangPref(): [Lang, (l: Lang) => void] {
  const [stored, setStored] = useState<Lang | undefined>(() => load().lang);
  const lang = stored ?? detectLang(navigator.languages ?? [navigator.language]);
  current = lang;
  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);
  const choose = (l: Lang) => {
    save({ lang: l });
    setStored(l);
  };
  return [lang, choose];
}
