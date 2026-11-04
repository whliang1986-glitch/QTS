// 告警与审计日志（S-05a）。读不到就明确显示“无法获取”，绝不显示成“没有告警”。
// 确认只停止重复提醒，不解除风险；确认由 guardd 执行（约一秒），界面下次刷新即可看到。
import { useCallback, useEffect, useState } from "react";
import { call } from "../api/client";
import type { AckResponse, AlertList, AlertView, AuditEntry, AuditList } from "../api/types.gen";
import { useLang, useT } from "../i18n";
import { Button, Empty, Notice, Tag, type Tone } from "../ui";

const LEVEL_TONE: Record<AlertView["level"], Tone> = { P0: "danger", P1: "warn", P2: "info", P3: "neutral" };

function time(iso: string | null): string {
  return iso ? iso.replace("T", " ").slice(0, 19) : "—";
}

type Feedback = { tone: "ok" | "info" | "danger"; text: string } | null;

export function Alerts({ canAck }: { canAck: boolean }) {
  const t = useT();
  const lang = useLang();
  const [list, setList] = useState<AlertList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [busy, setBusy] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<Record<number, Feedback>>({});

  const load = useCallback(async () => {
    const result = await call<AlertList>("GET", "/v1/alerts?limit=200");
    if (result.ok) {
      setList(result.data);
      setError(null);
    } else {
      setList(null); // 读不到就不显示旧列表
      setError(result.error);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => { if (alive) void load(); };
    tick();
    const timer = window.setInterval(tick, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [load]);

  const ack = async (alert: AlertView) => {
    setBusy(alert.alert_id);
    const result = await call<AckResponse>("POST", `/v1/alerts/${alert.alert_id}/ack`, undefined, 10000);
    setBusy(null);
    const fb: Feedback = result.ok
      ? { tone: result.data.already ? "info" : "ok", text: t(result.data.already ? "alerts.ackAlready" : "alerts.ackSent") }
      : { tone: "danger", text: t("alerts.ackFailed", { error: result.error }) };
    setFeedback((prev) => ({ ...prev, [alert.alert_id]: fb }));
    void load();
  };

  const shown = list === null ? [] : list.alerts.filter((a) => !onlyOpen || a.resolved_at === null);
  return (
    <>
      <div className="page-title"><h2>{t("alerts.title")}</h2><span className="hint">{t("alerts.refresh")}</span></div>
      <p className="hint" style={{ marginBottom: "var(--s3)" }}>{t("alerts.ackHint")}</p>
      {list === null ? (
        <div data-testid="alerts-unavailable">
          <Notice tone="warn" title={t("alerts.unavailable")}>
            {error ? `${error} · ` : ""}{t("alerts.unavailableHint")}
          </Notice>
        </div>
      ) : (
        <>
          <div className="row" style={{ marginBottom: "var(--s3)", gap: "var(--s3)" }}>
            <label className="row" style={{ gap: "var(--s2)" }}>
              <input type="checkbox" checked={onlyOpen} onChange={(e) => setOnlyOpen(e.target.checked)} data-testid="alerts-only-open" />
              {t("alerts.onlyOpen")}
            </label>
            {!canAck ? <span className="hint" data-testid="alerts-viewer">{t("alerts.viewer")}</span> : null}
          </div>
          {shown.length === 0 ? (
            <div className="card">
              <Empty icon="bell" title={t(onlyOpen ? "alerts.empty" : "alerts.emptyAll")} testId="alerts-empty">{t("alerts.emptyHint")}</Empty>
            </div>
          ) : (
            <div className="stack" data-testid="alerts-table">
              {shown.map((a) => {
                const fb = feedback[a.alert_id];
                const open = a.resolved_at === null && a.acked_by === null;
                return (
                  <div key={a.alert_id} className={`card alert-card alert-${a.level}`} data-testid={`alert-${a.alert_id}`}>
                    <div className="row alert-head">
                      <Tag tone={LEVEL_TONE[a.level]}>{a.level}</Tag>
                      {a.resolved_at !== null ? (
                        <Tag tone="ok">{t("alerts.state.resolved")}</Tag>
                      ) : a.acked_by !== null ? (
                        <Tag tone="info">{t("alerts.state.acked", { who: a.acked_by })}</Tag>
                      ) : (
                        <Tag tone={LEVEL_TONE[a.level] === "neutral" ? "info" : LEVEL_TONE[a.level]}>{t("alerts.state.open")}</Tag>
                      )}
                      <span className="spacer" />
                      <span className="hint num">{t("alerts.col.raised")} {time(a.raised_at)}</span>
                    </div>
                    <p className="alert-text">{(a.text[lang] ?? a.text.zh ?? a.code).replace(/^\[P\d\]\s*/, "")}</p>
                    {canAck && open ? (
                      <Button small kind="primary" icon="check" disabled={busy === a.alert_id}
                        onClick={() => void ack(a)} data-testid={`ack-${a.alert_id}`}>
                        {t("alerts.ack")}
                      </Button>
                    ) : null}
                    {fb ? <div className="hint" role="status" data-testid={`alert-feedback-${a.alert_id}`}>{fb.text}</div> : null}
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </>
  );
}

export function Audit() {
  const t = useT();
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<{ status: number; text: string } | null>(null);
  const [more, setMore] = useState(false);

  const load = useCallback(async (before?: number) => {
    const query = before === undefined ? "" : `&before=${before}`;
    const result = await call<AuditList>("GET", `/v1/audit?limit=100${query}`);
    if (result.ok) {
      setEntries((prev) => (before === undefined || prev === null ? result.data.entries : [...prev, ...result.data.entries]));
      setMore(result.data.entries.length === 100);
      setError(null);
    } else {
      if (before === undefined) setEntries(null);
      setError({ status: result.status, text: result.error });
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <>
      <div className="page-title"><h2>{t("audit.title")}</h2><span className="hint">{t("audit.refresh")}</span></div>
      {error !== null ? (
        <div data-testid="audit-unavailable" style={{ marginBottom: "var(--s3)" }}>
          <Notice tone="warn" title={t("audit.unavailable")}>
            {error.status === 403 ? t("audit.ownerOnly") : error.text}
          </Notice>
        </div>
      ) : null}
      {entries === null ? null : entries.length === 0 ? (
        <div className="card"><Empty icon="log" title={t("audit.empty")} testId="audit-empty" /></div>
      ) : (
        <>
          <div className="table-wrap">
            <table data-testid="audit-table">
              <thead>
                <tr>
                  <th scope="col">{t("audit.col.time")}</th><th scope="col">{t("audit.col.action")}</th>
                  <th scope="col">{t("audit.col.actor")}</th><th scope="col">{t("audit.col.detail")}</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => (
                  <tr key={e.seq}>
                    <td className="num">{time(e.at)}</td>
                    <td><code>{e.action}</code></td>
                    <td>{e.actor}</td>
                    <td className="hint">
                      {Object.entries(e.detail).map(([k, v]) => `${k}=${v}`).join(" · ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {more ? (
            <div style={{ marginTop: "var(--s3)" }}>
              <Button small onClick={() => void load(entries[entries.length - 1]?.seq)} data-testid="audit-more">{t("audit.more")}</Button>
            </div>
          ) : null}
        </>
      )}
    </>
  );
}
