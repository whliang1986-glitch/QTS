"""日终报告（S-09d）：当日订单与成交、日终对账、HALT 与告警，Markdown 和 JSON 两种格式。

- **一天按 UTC 日期算**：美股常规时段的开收盘落在同一 UTC 日期，日终对账也在这一天。
- **只读**：不写账本，不写文件，也不下任何命令。
- **不把“不知道”写成 0**：某部分取不到时，这部分的值是 None，原因记进 errors，报告标为不完整。
  条数到了上限也标为不完整，不静默截断。
- **日终对账**分四种：clean、not_clean、missing（这一天没有记录）、unknown（记录太多被截断，
  没看到日终那一条，不能说没做）。对账记录整个取不到时，这部分是 None。
  现金与佣金还要靠 Flex Query（R-07 后续），对账里的未核对项原样列出。
- 全部内容按诊断包同一套规则脱敏；金额和数量是字符串，不是浮点数。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol

from server.app.diagnostics import safe_error, scrub_value
from server.domain.enums import Side
from server.risk.reconcile import Code, ReconKind, recon_records_between
from server.storage.alertsdb import StoredAlert
from server.storage.ledger import MAX_VIEW_ROWS, LedgerRepository

Section = tuple[dict[str, Any], bool]  # 内容，以及是否完整（没有被截断）


class AlertSource(Protocol):
    def recent(self, limit: int) -> list[StoredAlert]: ...


class FutureDayError(ValueError):
    """这一天还没开始，没有报告可出。"""


@dataclass(frozen=True)
class DailyReport:
    day: date
    generated_at: datetime
    final: bool  # 这一天已经结束；否则数字还可能变化
    complete: bool  # 全部取到，也没有被截断
    data: Mapping[str, Any]  # 已脱敏，JSON 格式的内容
    markdown: str


def _orders(repo: LedgerRepository, start: datetime, end: datetime) -> Section:
    rows = repo.orders_created_between(start, end, MAX_VIEW_ROWS)
    by_status = Counter("NO_EVENTS" if r.status is None else r.status.value for r in rows)
    by_class = Counter(r.intent.order_class.value for r in rows)
    return {
        "count": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_class": dict(sorted(by_class.items())),
    }, len(rows) < MAX_VIEW_ROWS


def _executions(repo: LedgerRepository, start: datetime, end: datetime) -> Section:
    rows = repo.effective_executions_between(start, end, MAX_VIEW_ROWS)
    per_symbol: dict[str, dict[str, Decimal]] = {}
    commission = Decimal(0)
    for fill in rows:
        side = "buy" if fill.side is Side.BUY else "sell"
        entry = per_symbol.setdefault(
            fill.symbol,
            {"buy_quantity": Decimal(0), "buy_amount": Decimal(0),
             "sell_quantity": Decimal(0), "sell_amount": Decimal(0)},
        )  # fmt: skip
        entry[f"{side}_quantity"] += fill.quantity
        entry[f"{side}_amount"] += fill.quantity * fill.price
        commission += fill.commission
    return {
        "count": len(rows),
        "commission": commission,
        "by_symbol": dict(sorted(per_symbol.items())),
    }, len(rows) < MAX_VIEW_ROWS


def _recon_item(item: Mapping[str, str]) -> dict[str, str]:
    # 字段名换掉 key：脱敏规则会把名字像密钥的字段整个遮掉，这里的值只是标的或订单编号
    return {"code": item["code"], "status": item["status"],
            "subject": item["key"], "detail": item["detail"]}  # fmt: skip


def _reconciliation(repo: LedgerRepository, start: datetime, end: datetime) -> Section:
    records = recon_records_between(repo, start, end, MAX_VIEW_ROWS)  # 从新到旧
    whole = len(records) < MAX_VIEW_ROWS
    eod = next((r for r in records if r.kind is ReconKind.END_OF_DAY), None)
    summary: dict[str, Any] = {
        "end_of_day": "missing" if whole else "unknown",
        "intraday_runs": sum(r.kind is ReconKind.INTRADAY for r in records),
        "intraday_not_clean": sum(r.kind is ReconKind.INTRADAY and not r.clean for r in records),
        "items": [],
        "not_checked": [],
    }
    if eod is not None:
        items = [_recon_item(i) for i in eod.items]
        summary |= {
            "end_of_day": "clean" if eod.clean else "not_clean",
            "settled": eod.settled,
            "recon_id": eod.recon_id,
            "at": eod.at,
            "items": [i for i in items if i["code"] != Code.NOT_RECONCILED.value],
            "not_checked": [i for i in items if i["code"] == Code.NOT_RECONCILED.value],
        }
    return summary, whole


def _modes(repo: LedgerRepository, start: datetime, end: datetime) -> Section:
    changes = repo.mode_changes_between(start, end, MAX_VIEW_ROWS)
    last = repo.latest_mode_before(end)
    return {
        "at_day_end": None if last is None else last.mode.value,
        "halted_at_day_end": None if last is None else last.halt.halted,
        "changes": [
            {"at": c.changed_at, "mode": c.mode.value, "halted": c.halt.halted,
             "reason": c.halt.reason, "actor": c.actor}
            for c in changes
        ],
    }, len(changes) < MAX_VIEW_ROWS  # fmt: skip


def _alerts(source: AlertSource | None, start: datetime, end: datetime) -> Section:
    if source is None:
        raise RuntimeError("没有配置告警库")
    rows = source.recent(MAX_VIEW_ROWS)  # 从新到旧
    today = [a for a in rows if start <= datetime.fromisoformat(a.raised_at) < end]
    # 取到上限，而最旧的一条还没早过这一天：这一天更早的可能被截掉了
    whole = len(rows) < MAX_VIEW_ROWS or datetime.fromisoformat(rows[-1].raised_at) < start
    return {
        "count": len(today),
        "by_level": dict(sorted(Counter(a.level for a in today).items())),
        "unresolved": sum(a.resolved_at is None for a in today),
        "items": [
            {"level": a.level, "code": a.code, "raised_at": a.raised_at,
             "acked_by": a.acked_by, "resolved_at": a.resolved_at, "params": dict(a.params)}
            for a in today
        ],
    }, whole  # fmt: skip


# ---------- Markdown（只用已脱敏的 data，不另外取数） ----------


def _counts(counts: Mapping[str, int]) -> str:
    return "、".join(f"{k} {v}" for k, v in counts.items()) or "无"


def _md_orders(orders: Mapping[str, Any]) -> list[str]:
    return [
        f"- 当日生成：{orders['count']}",
        f"- 按最新状态：{_counts(orders['by_status'])}",
        f"- 按类别：{_counts(orders['by_class'])}",
    ]


def _md_executions(fills: Mapping[str, Any]) -> list[str]:
    lines = [f"- 笔数：{fills['count']}", f"- 佣金合计：{fills['commission']}"]
    if fills["by_symbol"]:
        lines += [
            "",
            "| 标的 | 买入数量 | 买入金额 | 卖出数量 | 卖出金额 |",
            "|---|---|---|---|---|",
        ]
    for symbol, e in fills["by_symbol"].items():
        lines.append(
            f"| {symbol} | {e['buy_quantity']} | {e['buy_amount']} "
            f"| {e['sell_quantity']} | {e['sell_amount']} |"
        )
    return lines


_EOD_TEXT = {
    "clean": "通过（没有未解释的差异）",
    "not_clean": "**未通过（有未解释的差异）**",
    "missing": "**这一天没有日终对账记录**",
    "unknown": "**未知（记录太多被截断，没有看到日终那一条）**",
}


def _md_reconciliation(recon: Mapping[str, Any]) -> list[str]:
    return [
        f"- 结果：{_EOD_TEXT[recon['end_of_day']]}",
        f"- 盘中对账：{recon['intraday_runs']} 次，未通过 {recon['intraday_not_clean']} 次",
        *(f"- 差异 {i['code']} / {i['status']} / {i['subject']}：{i['detail']}"
          for i in recon["items"]),
        *(f"- 未核对：{i['detail']}" for i in recon["not_checked"]),
    ]  # fmt: skip


def _md_modes(modes: Mapping[str, Any]) -> list[str]:
    halted = {True: "是", False: "否", None: "未知"}[modes["halted_at_day_end"]]
    lines = [f"- 日末模式：{modes['at_day_end'] or '未知'}", f"- 日末是否 HALT：{halted}"]
    for c in modes["changes"]:
        reason = f"：{c['reason']}" if c["reason"] else ""
        state = "HALT" if c["halted"] else "未 HALT"
        lines.append(f"- {c['at']} → {c['mode']}（{state}，{c['actor']}）{reason}")
    return lines


def _md_alerts(alerts: Mapping[str, Any]) -> list[str]:
    return [
        f"- 当日发生：{alerts['count']}（未解除 {alerts['unresolved']}）",
        f"- 按级别：{_counts(alerts['by_level'])}",
        *(f"- {a['raised_at']} {a['level']} {a['code']}" for a in alerts["items"]),
    ]


_MARKDOWN: tuple[tuple[str, str, Callable[[Mapping[str, Any]], list[str]]], ...] = (
    ("订单", "orders", _md_orders),
    ("成交（去掉被更正取代的）", "executions", _md_executions),
    ("日终对账", "recon_result", _md_reconciliation),
    ("模式与 HALT", "modes", _md_modes),
    ("告警", "alerts", _md_alerts),
)


def _render(data: Mapping[str, Any]) -> str:
    errors: Mapping[str, str] = data["errors"]
    out = [f"# 日终报告 {data['day']}（UTC 日期）", "", f"生成时间（UTC）：{data['generated_at']}"]
    if not data["final"]:
        out += ["", "**这一天还没结束，下面的数字还可能变化。**"]
    if not data["complete"]:
        missing = "、".join([*errors, *data["truncated"]])
        out += ["", f"**报告不完整**：{missing}。缺的部分不等于 0。"]
    for title, name, render in _MARKDOWN:
        value = data[name]
        body = [f"**取不到**：{errors.get(name, '')}"] if value is None else render(value)
        out += ["", f"## {title}", *body]
        if name in data["truncated"]:
            out.append("- **条数到了上限，上面的统计不完整**")
    return "\n".join(out) + "\n"


# ---------- 入口 ----------


def build_daily_report(
    day: date,
    *,
    ledger: Callable[[], LedgerRepository],
    alerts: AlertSource | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DailyReport:
    if isinstance(day, datetime) or not isinstance(day, date):
        raise ValueError("day 必须是 date")
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("日终报告时钟必须带时区")
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    if now < start:
        raise FutureDayError(f"{day} 还没开始")

    def from_ledger(read: Callable[[LedgerRepository, datetime, datetime], Section]) -> Any:
        return lambda: read(ledger(), start, end)

    sources: dict[str, Callable[[], Section]] = {
        "orders": from_ledger(_orders),
        "executions": from_ledger(_executions),
        "recon_result": from_ledger(_reconciliation),
        "modes": from_ledger(_modes),
        "alerts": lambda: _alerts(alerts, start, end),
    }
    content: dict[str, dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    truncated: list[str] = []
    for name, read in sources.items():
        try:
            content[name], whole = read()
        except Exception as exc:  # 这一部分取不到：记下来，其他部分照常
            content[name] = None
            errors[name] = safe_error(exc)
        else:
            if not whole:
                truncated.append(name)
    final, complete = now >= end, not errors and not truncated
    data: dict[str, Any] = scrub_value({
        "day": day,
        "generated_at": now,
        "final": final,
        "complete": complete,
        **content,
        "errors": errors,
        "truncated": truncated,
    })  # fmt: skip
    return DailyReport(day, now, final, complete, data, _render(data))


__all__ = ("AlertSource", "DailyReport", "FutureDayError", "build_daily_report")
