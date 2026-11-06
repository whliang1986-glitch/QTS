// 诊断包（S-09c）：先预览全部内容，确认看过才能下载，下载的就是预览过的那一份。
// 内容只放在内存里（不进存储、不进 URL）；离开页面、令牌失效、到期都会丢弃。
// 脱敏是尽力而为，人工预览是最后一道关。“我已看完”是界面上的门槛，服务端不验证。
import { useEffect, useRef, useState } from "react";
import { call, download } from "../api/client";
import type { DiagnosticsPreview } from "../api/types.gen";
import { useT } from "../i18n";
import { Button, Notice } from "../ui";

const ZIP_TYPE = "application/zip";

function save(blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "qts-diagnostics.zip";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 10000); // 浏览器开始下载之后才能回收
}

/** 代理或异常回答可能结构不对：不校验的话渲染时报错，整个控制台（含急停页）会白屏。 */
function isPreview(data: unknown): data is DiagnosticsPreview {
  if (typeof data !== "object" || data === null) return false;
  const p = data as Partial<DiagnosticsPreview>;
  return (
    typeof p.preview_id === "string" &&
    typeof p.created_at === "string" &&
    typeof p.expires_in_seconds === "number" &&
    typeof p.errors === "object" && p.errors !== null &&
    Object.values(p.errors).every((v) => typeof v === "string") &&
    Array.isArray(p.files) &&
    p.files.every((f) => typeof f?.name === "string" && typeof f?.content === "string")
  );
}

export function Diagnostics({ canUse }: { canUse: boolean }) {
  const t = useT();
  const [preview, setPreview] = useState<DiagnosticsPreview | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const pending = useRef<AbortController | null>(null);

  // 离开页面或退出登录：取消在途下载，文件不会在这之后落盘
  useEffect(() => () => pending.current?.abort(), []);

  useEffect(() => {
    if (preview === null) return undefined;
    const timer = window.setTimeout(() => {
      setPreview(null); // 到期后内容不再留在页面上
      setExpired(true);
    }, preview.expires_in_seconds * 1000);
    return () => window.clearTimeout(timer);
  }, [preview]);

  const generate = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    setReviewed(false);
    setExpired(false);
    setPreview(null); // 失败时不留旧的那份
    const result = await call<unknown>("POST", "/v1/diagnostics/previews");
    setBusy(false);
    if (!result.ok) setError(t("diag.failed", { error: result.error }));
    else if (isPreview(result.data)) setPreview(result.data);
    else setError(t("diag.failed", { error: t("diag.badResponse") }));
  };

  const fetchArchive = async (id: string) => {
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError(null);
    setSaved(false);
    const result = await download(`/v1/diagnostics/previews/${encodeURIComponent(id)}/archive`, controller.signal);
    if (controller.signal.aborted) return; // 已离开页面：不存文件，也不再更新界面
    setBusy(false);
    if (result.ok) {
      if (result.data.type === ZIP_TYPE) {
        save(result.data);
        setSaved(true);
      } else {
        setError(t("diag.downloadFailed", { error: t("diag.badResponse") }));
      }
    } else if (result.status === 401) {
      setPreview(null); // 令牌失效：内容立刻不再显示，不等状态轮询
      setError(t("diag.downloadFailed", { error: result.error }));
    } else if (result.status === 404) {
      setPreview(null); // 过期或被更新的预览挤掉：只能重新预览
      setExpired(true);
    } else {
      setError(t("diag.downloadFailed", { error: result.error }));
    }
  };

  if (!canUse) {
    return (
      <>
        <div className="page-title"><h2>{t("diag.title")}</h2></div>
        <div data-testid="diag-owner-only"><Notice tone="info" title={t("diag.ownerOnly")}>{t("diag.ownerOnlyHint")}</Notice></div>
      </>
    );
  }
  const errors = preview === null ? [] : Object.entries(preview.errors);
  return (
    <>
      <div className="page-title"><h2>{t("diag.title")}</h2><span className="hint">{t("diag.hint")}</span></div>
      <div className="row" style={{ marginBottom: "var(--s3)" }}>
        <Button kind="primary" icon="log" disabled={busy} onClick={() => void generate()} data-testid="diag-generate">
          {t(preview === null ? "diag.generate" : "diag.regenerate")}
        </Button>
      </div>
      {error !== null ? (
        <div data-testid="diag-error" style={{ marginBottom: "var(--s3)" }}><Notice tone="danger" title={t("diag.errorTitle")}>{error}</Notice></div>
      ) : null}
      {saved ? <p className="hint" role="status" data-testid="diag-saved">{t("diag.saved")}</p> : null}
      {expired ? (
        <div data-testid="diag-expired"><Notice tone="warn" title={t("diag.expired")}>{t("diag.expiredHint")}</Notice></div>
      ) : null}
      {preview !== null ? (
        <div className="stack">
          <Notice tone="warn" title={t("diag.reviewTitle")}>{t("diag.reviewHint")}</Notice>
          {errors.length > 0 ? (
            <div data-testid="diag-source-errors">
              <Notice tone="warn" title={t("diag.sourceErrors")}>
                {errors.map(([name, text]) => <div key={name}><code>{name}</code>：{text}</div>)}
              </Notice>
            </div>
          ) : null}
          <div className="hint num" data-testid="diag-meta">
            {t("diag.meta", { at: preview.created_at.replace("T", " ").slice(0, 19), count: preview.files.length })}
          </div>
          {preview.files.map((file) => (
            <details key={file.name} className="card diag-file" open data-testid={`diag-file-${file.name}`}>
              <summary><code>{file.name}</code></summary>
              <pre>{file.content}</pre>
            </details>
          ))}
          <div className="card row" style={{ gap: "var(--s3)" }}>
            <label className="row" style={{ gap: "var(--s2)" }}>
              <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} data-testid="diag-reviewed" />
              {t("diag.reviewed")}
            </label>
            <Button icon="download" disabled={!reviewed || busy} onClick={() => void fetchArchive(preview.preview_id)} data-testid="diag-download">
              {t("diag.download")}
            </Button>
          </div>
        </div>
      ) : null}
    </>
  );
}
