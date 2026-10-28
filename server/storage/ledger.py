"""账本连接与 Repository（C-03，V4.1 5.6）。

- 连接参数：WAL、synchronous=FULL、foreign_keys=ON、recursive_triggers=ON、busy_timeout；
  WAL 设置不成功即拒绝打开（例如账本放在了网络盘上）。
- 只追加：Repository 只有“追加”与“查询”方法；数据库触发器禁止 UPDATE、DELETE 与 REPLACE。
  打开账本时逐一核对迁移版本与全部触发器，缺一个就拒绝。
- 幂等：同一个键再次写入完全相同的内容返回 False；内容不同抛 ConflictError，绝不静默覆盖。
- 失败即关闭：读写中的任何数据库错误、损坏的记录都抛 StorageError，不吞掉。
- 单写者：ledger.db 只由 tradingd 写，其他进程用 read_only=True 的只读连接（S-01 落实到进程）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, Row, Table, create_engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.pool import NullPool

from server.domain.enums import Mode
from server.domain.modes import AuditEvent, HaltState
from server.domain.order_state import OrderEvent, OrderStatus
from server.domain.orders import BrokerOrder, Execution, OrderIntent, RiskDecision
from server.storage import schema
from server.storage.errors import ConflictError, StorageError
from server.storage.migrate import head_revision

LEDGER_TABLES = schema.LEDGER_TABLES


def open_ledger(path: Path, *, read_only: bool = False, busy_timeout_ms: int = 5000) -> Engine:
    """打开账本。读写连接强制 WAL / FULL / 外键；只读连接以 SQLite 只读模式打开并禁止写入。"""
    timeout = busy_timeout_ms / 1000

    def connect() -> sqlite3.Connection:
        if read_only:
            uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=timeout)
        try:
            apply_pragmas(conn, read_only=read_only, busy_timeout_ms=busy_timeout_ms)
        except Exception:
            conn.close()
            raise
        return conn

    return create_engine("sqlite+pysqlite://", creator=connect, poolclass=NullPool)


def apply_pragmas(conn: sqlite3.Connection, *, read_only: bool, busy_timeout_ms: int) -> None:
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    # REPLACE 先删后插：打开递归触发器后，删除也会触发 _no_delete（第二层防线）
    conn.execute("PRAGMA recursive_triggers = ON")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
        return
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    if not mode or str(mode[0]).lower() != "wal":
        raise StorageError(f"无法启用 WAL（得到 {mode}）；账本必须放在本地磁盘")
    conn.execute("PRAGMA synchronous = FULL")


def _iso(value: datetime) -> str:
    """定长 UTC ISO 字符串（固定到微秒），保证按字符串排序即按时间排序。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("写入账本的时间必须带时区")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class CommandRecord:
    request_id: str
    command: str
    result: str
    recorded_at: str


@dataclass(frozen=True)
class Issuance:
    """一次票据签发：券商订单、用掉的风控决定与实际下单数量（O-01）。"""

    broker_order: BrokerOrder
    decision_id: str
    quantity: Decimal
    issued_at: str


@dataclass(frozen=True)
class Amendment:
    """一次改单请求及其当前结果（O-03）。result 为空表示券商调用之前就停下了（崩溃窗口）。"""

    amendment_id: str
    intent_id: str
    decision_id: str
    new_price: Decimal
    new_quantity: Decimal
    requested_at: str
    result: str | None


@dataclass(frozen=True)
class PositionAdjustment:
    """一条持仓调整（R-07）：不来自本系统成交的持仓变化。kind 见迁移 0006。"""

    adjustment_id: str
    account_id: str
    symbol: str
    quantity_delta: Decimal
    kind: str
    reference: str
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class ModeChange:
    change_id: str
    mode: Mode
    halt: HaltState
    changed_at: str
    actor: str


class LedgerRepository:
    """ledger.db 的唯一访问入口。只追加、只查询。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._require_intact()

    # ---------- 基础设施 ----------

    @contextmanager
    def _reading(self, what: str) -> Iterator[Connection]:
        """读操作：数据库错误与损坏的记录一律转成 StorageError。"""
        try:
            with self._engine.connect() as conn:
                yield conn
        except StorageError:
            raise
        except (DBAPIError, ValidationError, ValueError) as exc:
            raise StorageError(f"读取 {what} 失败：{exc}") from exc

    def check_integrity(self) -> None:
        """健康检查（R-08）：迁移版本与全部只追加触发器都在；不完整抛 StorageError。"""
        self._require_intact()

    def _require_intact(self) -> None:
        with self._reading("账本版本") as conn:
            try:
                version = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            except DBAPIError as exc:
                raise StorageError("账本未迁移：请先运行 server.storage.migrate.upgrade") from exc
            # 名字、所属表、触发器体都要对：同名挂错表或被改成空操作也算缺失
            triggers = {
                name
                for name, table, sql in conn.exec_driver_sql(
                    "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
                )
                if name.rsplit("_no_", 1)[0] == table and "RAISE(ABORT" in (sql or "")
            }
        if version != head_revision():
            raise StorageError(f"账本迁移版本 {version} 不是最新 {head_revision()}，请先迁移")
        missing = schema.LEDGER_TRIGGERS - triggers
        if missing:
            raise StorageError(f"账本只追加触发器缺失：{sorted(missing)}；拒绝打开")

    def _fetch(self, table: Table, where: Mapping[str, object]) -> Row[Any] | None:
        stmt = select(table)
        for column, value in where.items():
            stmt = stmt.where(table.c[column] == value)
        with self._reading(table.name) as conn:
            return conn.execute(stmt).first()

    def _insert_once(
        self,
        table: Table,
        row: Mapping[str, object],
        keys: list[Mapping[str, object]],
        same: Callable[[Row[Any]], bool],
    ) -> bool:
        """插入一行。键已存在时：内容相同返回 False，不同抛 ConflictError。

        回查不存在竞态：账本不删行、单写者，冲突行一旦存在就一定已提交且不会消失。
        """
        try:
            with self._engine.begin() as conn:
                conn.execute(table.insert().values(**row))
        except IntegrityError as exc:
            for key in keys:
                existing = self._fetch(table, key)
                if existing is not None:
                    if same(existing):
                        return False
                    raise ConflictError(f"{table.name} 中 {dict(key)} 已存在且内容不同") from exc
            raise StorageError(f"写入 {table.name} 被约束拒绝：{exc.orig}") from exc
        except DBAPIError as exc:
            raise StorageError(f"写入 {table.name} 失败：{exc.orig}") from exc
        return True

    # ---------- 订单意图 ----------

    def add_intent(self, intent: OrderIntent) -> bool:
        payload = intent.model_dump_json()
        return self._insert_once(
            schema.orders,
            {
                "intent_id": intent.intent_id,
                "account_id": intent.account_id,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "order_class": intent.order_class.value,
                "created_at": _iso(intent.created_at),
                "payload": payload,
            },
            [{"intent_id": intent.intent_id}],
            lambda r: r.payload == payload,
        )

    def get_intent(self, intent_id: str) -> OrderIntent | None:
        row = self._fetch(schema.orders, {"intent_id": intent_id})
        if row is None:
            return None
        with self._reading("orders"):
            return OrderIntent.model_validate_json(row.payload)

    # ---------- 券商订单与 permId ----------

    def add_broker_order(self, order: BrokerOrder) -> bool:
        if order.perm_id is not None:
            raise StorageError("permId 通过 record_perm_id 单独登记")
        payload = order.model_dump_json()
        return self._insert_once(
            schema.broker_orders,
            {
                "broker_order_id": order.broker_order_id,
                "intent_id": order.intent_id,
                "order_class": order.order_class.value,
                "submitted_at": _iso(order.submitted_at),
                "payload": payload,
            },
            [{"broker_order_id": order.broker_order_id}, {"intent_id": order.intent_id}],
            lambda r: r.payload == payload,
        )

    def record_perm_id(self, intent_id: str, perm_id: str, at: datetime) -> bool:
        """permId 只能登记一次：一个意图一个 permId，一个 permId 只属于一个意图。"""
        return self._insert_once(
            schema.perm_ids,
            {"perm_id": perm_id, "intent_id": intent_id, "recorded_at": _iso(at)},
            [{"perm_id": perm_id}, {"intent_id": intent_id}],
            lambda r: r.perm_id == perm_id and r.intent_id == intent_id,
        )

    # ---------- 订单事件 ----------

    def append_order_event(self, event: OrderEvent, status: OrderStatus) -> bool:
        """记录一条订单事件及其后的状态。event_id 是幂等键：重试不会写出重复事件。

        调用方必须先用 transition() 算出 status（O-01），存储层不判断转移。
        """
        payload = event.model_dump_json()
        return self._insert_once(
            schema.order_events,
            {
                "event_id": event.event_id,
                "intent_id": event.intent_id,
                "kind": event.kind.value,
                "status": OrderStatus(status).value,
                "occurred_at": _iso(event.occurred_at),
                "correlation_id": event.correlation_id,
                "payload": payload,
            },
            [{"event_id": event.event_id}],
            lambda r: r.payload == payload and r.status == OrderStatus(status).value,
        )

    def order_history(self, intent_id: str) -> list[tuple[OrderEvent, OrderStatus]]:
        t = schema.order_events
        stmt = select(t.c.payload, t.c.status).where(t.c.intent_id == intent_id).order_by(t.c.seq)
        with self._reading("order_events") as conn:
            return [
                (OrderEvent.model_validate_json(p), OrderStatus(s)) for p, s in conn.execute(stmt)
            ]

    def latest_status(self, intent_id: str) -> OrderStatus | None:
        history = self.order_history(intent_id)
        return history[-1][1] if history else None

    # ---------- 成交 ----------

    @staticmethod
    def _execution_row(execution: Execution) -> dict[str, object]:
        return {
            "execution_id": execution.execution_id,
            "intent_id": execution.intent_id,
            "broker_order_id": execution.broker_order_id,
            "symbol": execution.symbol,
            "side": execution.side.value,
            "quantity": str(execution.quantity),
            "price": str(execution.price),
            "commission": str(execution.commission),
            "executed_at": _iso(execution.executed_at),
            "payload": execution.model_dump_json(),
        }

    def add_execution(self, execution: Execution) -> bool:
        row = self._execution_row(execution)
        payload = row["payload"]
        return self._insert_once(
            schema.executions,
            row,
            [{"execution_id": execution.execution_id}],
            lambda r: r.payload == payload,
        )

    def add_correction(self, correction: Execution, superseded_id: str, at: datetime) -> bool:
        """成交更正：更正后的成交另起一行，并记录它取代了哪一笔（同一事务）。

        完全相同的更正再写一次返回 False；
        被取代的成交已被别的更正取代、或更正行内容不同抛 ConflictError。
        """
        payload = correction.model_dump_json()
        try:
            with self._engine.begin() as conn:
                conn.execute(schema.executions.insert().values(**self._execution_row(correction)))
                conn.execute(
                    schema.execution_supersessions.insert().values(
                        superseded_execution_id=superseded_id,
                        superseding_execution_id=correction.execution_id,
                        recorded_at=_iso(at),
                    )
                )
        except IntegrityError as exc:
            row = self._fetch(schema.executions, {"execution_id": correction.execution_id})
            link = self._fetch(
                schema.execution_supersessions, {"superseded_execution_id": superseded_id}
            )
            if (
                row is not None
                and row.payload == payload
                and link is not None
                and link.superseding_execution_id == correction.execution_id
            ):
                return False
            raise ConflictError(
                f"成交更正冲突：{superseded_id} 已被取代，或更正 {correction.execution_id} 内容不同"
            ) from exc
        except DBAPIError as exc:
            raise StorageError(f"写入成交更正失败：{exc.orig}") from exc
        return True

    def superseding_of(self, execution_id: str) -> str | None:
        """取代某笔成交的更正（没有则 None）。"""
        row = self._fetch(schema.execution_supersessions, {"superseded_execution_id": execution_id})
        return None if row is None else str(row.superseding_execution_id)

    def has_execution(self, execution_id: str) -> bool:
        return self._fetch(schema.executions, {"execution_id": execution_id}) is not None

    def superseded_executions(self, intent_id: str) -> set[str]:
        """某意图下已被更正取代的成交。"""
        t, e = schema.execution_supersessions, schema.executions
        stmt = (
            select(t.c.superseded_execution_id)
            .join(e, e.c.execution_id == t.c.superseded_execution_id)
            .where(e.c.intent_id == intent_id)
        )
        with self._reading("execution_supersessions") as conn:
            return {str(i) for (i,) in conn.execute(stmt)}

    def effective_executions_for(self, intent_id: str) -> list[Execution]:
        """有效成交：全部成交去掉被更正取代的（单条查询，只读进程不会读到半套更正）。"""
        e, t = schema.executions, schema.execution_supersessions
        superseded = select(t.c.superseded_execution_id).where(
            t.c.superseded_execution_id == e.c.execution_id
        )
        stmt = (
            select(e.c.payload)
            .where(e.c.intent_id == intent_id, ~superseded.exists())
            .order_by(e.c.executed_at, e.c.execution_id)
        )
        with self._reading("executions") as conn:
            return [Execution.model_validate_json(p) for (p,) in conn.execute(stmt)]

    def executions_for(self, intent_id: str) -> list[Execution]:
        t = schema.executions
        stmt = (
            select(t.c.payload)
            .where(t.c.intent_id == intent_id)
            .order_by(t.c.executed_at, t.c.execution_id)
        )
        with self._reading("executions") as conn:
            return [Execution.model_validate_json(p) for (p,) in conn.execute(stmt)]

    # ---------- 票据签发（O-01） ----------

    def commit_submission(
        self,
        order: BrokerOrder,
        decision_id: str,
        quantity: Decimal,
        event: OrderEvent,
        status: OrderStatus,
    ) -> bool:
        """在下单之前，把券商订单、签发记录与 SUBMIT_SENT 事件放在同一事务写入。

        完全相同的签发再写一次返回 False（重试安全）；意图、订单号或风控决定已被别的签发占用抛
        ConflictError——一个决定只能用一次，一个意图只有一条订单链。
        """
        if order.perm_id is not None:
            raise StorageError("permId 通过 record_perm_id 单独登记")
        if event.intent_id != order.intent_id:
            raise StorageError("签发事件与券商订单不属于同一意图")
        order_payload = order.model_dump_json()
        event_payload = event.model_dump_json()
        status_value = OrderStatus(status).value
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    schema.broker_orders.insert().values(
                        broker_order_id=order.broker_order_id,
                        intent_id=order.intent_id,
                        order_class=order.order_class.value,
                        submitted_at=_iso(order.submitted_at),
                        payload=order_payload,
                    )
                )
                conn.execute(
                    schema.ticket_issuance.insert().values(
                        broker_order_id=order.broker_order_id,
                        intent_id=order.intent_id,
                        decision_id=decision_id,
                        quantity=str(quantity),
                        issued_at=_iso(order.submitted_at),
                    )
                )
                conn.execute(
                    schema.order_events.insert().values(
                        event_id=event.event_id,
                        intent_id=event.intent_id,
                        kind=event.kind.value,
                        status=status_value,
                        occurred_at=_iso(event.occurred_at),
                        correlation_id=event.correlation_id,
                        payload=event_payload,
                    )
                )
        except IntegrityError as exc:
            existing = self.issuance_for(order.intent_id)
            same = (
                existing is not None
                and existing.broker_order.model_dump_json() == order_payload
                and existing.decision_id == decision_id
                and existing.quantity == quantity
            )
            row = self._fetch(schema.order_events, {"event_id": event.event_id})
            if same and row is not None and row.payload == event_payload:
                return False
            raise ConflictError(
                f"签发冲突：意图 {order.intent_id}、订单号 {order.broker_order_id} 或风控决定 "
                f"{decision_id} 已被占用"
            ) from exc
        except DBAPIError as exc:
            raise StorageError(f"写入签发失败：{exc.orig}") from exc
        return True

    def issuance_for(self, intent_id: str) -> Issuance | None:
        row = self._fetch(schema.ticket_issuance, {"intent_id": intent_id})
        if row is None:
            return None
        order_row = self._fetch(schema.broker_orders, {"broker_order_id": row.broker_order_id})
        if order_row is None:  # pragma: no cover - 外键保证
            raise StorageError(f"签发记录 {row.broker_order_id} 缺少券商订单")
        with self._reading("ticket_issuance"):
            return Issuance(
                broker_order=BrokerOrder.model_validate_json(order_row.payload),
                decision_id=row.decision_id,
                quantity=Decimal(row.quantity),
                issued_at=row.issued_at,
            )

    def intent_for_broker_order(self, broker_order_id: str) -> str | None:
        row = self._fetch(schema.broker_orders, {"broker_order_id": broker_order_id})
        return None if row is None else str(row.intent_id)

    def submitted_intents(self) -> list[str]:
        """全部已签发（交给过券商）的意图，按签发顺序。"""
        t = schema.ticket_issuance
        stmt = select(t.c.intent_id).order_by(t.c.issued_at, t.c.intent_id)
        with self._reading("ticket_issuance") as conn:
            return [str(i) for (i,) in conn.execute(stmt)]

    def add_position_adjustment(self, adjustment: PositionAdjustment) -> bool:
        """持仓调整（R-07）：只追加；同一 (kind, reference) 只能一次，内容不同抛 ConflictError。"""
        payload = json.dumps(
            {
                "adjustment_id": adjustment.adjustment_id,
                "account_id": adjustment.account_id,
                "symbol": adjustment.symbol,
                "quantity_delta": str(adjustment.quantity_delta),
                "kind": adjustment.kind,
                "reference": adjustment.reference,
                "actor": adjustment.actor,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return self._insert_once(
            schema.position_adjustments,
            {
                "adjustment_id": adjustment.adjustment_id,
                "account_id": adjustment.account_id,
                "symbol": adjustment.symbol,
                "quantity_delta": str(adjustment.quantity_delta),
                "kind": adjustment.kind,
                "reference": adjustment.reference,
                "actor": adjustment.actor,
                "recorded_at": _iso(adjustment.recorded_at),
                "payload": payload,
            },
            [
                {"adjustment_id": adjustment.adjustment_id},
                {"kind": adjustment.kind, "reference": adjustment.reference},
            ],
            lambda r: r.payload == payload,
        )

    def position_adjustments(self, account_id: str) -> list[PositionAdjustment]:
        """某账户的全部持仓调整，按写入顺序。"""
        t = schema.position_adjustments
        stmt = select(t).where(t.c.account_id == account_id).order_by(t.c.seq)
        with self._reading("position_adjustments") as conn:
            rows = list(conn.execute(stmt))
        return [
            PositionAdjustment(
                adjustment_id=row.adjustment_id,
                account_id=row.account_id,
                symbol=row.symbol,
                quantity_delta=Decimal(row.quantity_delta),
                kind=row.kind,
                reference=row.reference,
                actor=row.actor,
                recorded_at=datetime.fromisoformat(row.recorded_at),
            )
            for row in rows
        ]

    def max_numeric_broker_order_id(self) -> int:
        """账本里最大的数字订单号（没有则 0）；下一个订单号取 max(券商, 账本 + 1)。"""
        stmt = select(schema.broker_orders.c.broker_order_id)
        with self._reading("broker_orders") as conn:
            ids = [str(i) for (i,) in conn.execute(stmt)]
        return max((int(i) for i in ids if i.isdigit()), default=0)

    def perm_id_for(self, intent_id: str) -> str | None:
        row = self._fetch(schema.perm_ids, {"intent_id": intent_id})
        return None if row is None else str(row.perm_id)

    def add_unmatched_execution(
        self, execution_id: str, reason: str, payload: str, at: datetime
    ) -> bool:
        """无法关联到本系统订单的成交照实入账（payload 是券商侧成交事实的 JSON）。"""
        return self._insert_once(
            schema.unmatched_executions,
            {
                "execution_id": execution_id,
                "reason": reason,
                "recorded_at": _iso(at),
                "payload": payload,
            },
            [{"execution_id": execution_id}],
            lambda r: r.payload == payload,
        )

    def unmatched_executions(self) -> list[tuple[str, str, str]]:
        """(execution_id, reason, payload)，按入账顺序。"""
        t = schema.unmatched_executions
        stmt = select(t.c.execution_id, t.c.reason, t.c.payload).order_by(
            t.c.recorded_at, t.c.execution_id
        )
        with self._reading("unmatched_executions") as conn:
            return [(str(e), str(r), str(p)) for e, r, p in conn.execute(stmt)]

    # ---------- 改单（O-03） ----------

    def add_amendment(  # noqa: PLR0913 - 改单请求的字段全部显式给出
        self,
        *,
        amendment_id: str,
        intent_id: str,
        decision_id: str,
        new_price: Decimal,
        new_quantity: Decimal,
        at: datetime,
    ) -> bool:
        """改单请求在调用券商之前落账。

        相同请求重写返回 False；决定已被别的改单用过抛 ConflictError。
        """
        row = {
            "amendment_id": amendment_id,
            "intent_id": intent_id,
            "decision_id": decision_id,
            "new_price": str(new_price),
            "new_quantity": str(new_quantity),
            "requested_at": _iso(at),
        }
        same = (amendment_id, intent_id, decision_id, str(new_price), str(new_quantity))
        return self._insert_once(
            schema.order_amendments,
            row,
            [{"amendment_id": amendment_id}, {"decision_id": decision_id}],
            lambda r: (
                (r.amendment_id, r.intent_id, r.decision_id, r.new_price, r.new_quantity) == same
            ),
        )

    def record_amendment_result(self, amendment_id: str, result: str, at: datetime) -> bool:
        """追加一条改单结果。与当前结果相同返回 False（重放）；先后关系不合法抛 ConflictError。

        合法的先后：无 → 任意；UNKNOWN → ACCEPTED / REJECTED；ACCEPTED → REJECTED（券商异步拒绝）。
        """
        if self.amendment_result(amendment_id) == result:
            return False
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    schema.order_amendment_results.insert().values(
                        amendment_id=amendment_id, result=result, recorded_at=_iso(at)
                    )
                )
        except IntegrityError as exc:
            current = self.amendment_result(amendment_id)
            raise ConflictError(
                f"改单 {amendment_id} 的结果不能从 {current} 变为 {result}"
            ) from exc
        except DBAPIError as exc:
            raise StorageError(f"写入改单结果失败：{exc.orig}") from exc
        return True

    def amendment_result(self, amendment_id: str) -> str | None:
        """当前结果（最后一条）；没有结果返回 None。"""
        t = schema.order_amendment_results
        stmt = (
            select(t.c.result)
            .where(t.c.amendment_id == amendment_id)
            .order_by(t.c.seq.desc())
            .limit(1)
        )
        with self._reading("order_amendment_results") as conn:
            value = conn.execute(stmt).scalar()
        return None if value is None else str(value)

    def amendment_owner(self, amendment_id: str) -> str | None:
        """amendment_id 属于哪个意图（全局唯一）。"""
        row = self._fetch(schema.order_amendments, {"amendment_id": amendment_id})
        return None if row is None else str(row.intent_id)

    def amendments_for(self, intent_id: str) -> list[Amendment]:
        """某意图的全部改单，按请求顺序。"""
        a = schema.order_amendments
        stmt = select(a).where(a.c.intent_id == intent_id).order_by(a.c.seq)
        with self._reading("order_amendments") as conn:
            rows = list(conn.execute(stmt))
        return [
            Amendment(
                amendment_id=row.amendment_id,
                intent_id=row.intent_id,
                decision_id=row.decision_id,
                new_price=Decimal(row.new_price),
                new_quantity=Decimal(row.new_quantity),
                requested_at=row.requested_at,
                result=self.amendment_result(row.amendment_id),
            )
            for row in rows
        ]

    def decision_is_used(self, decision_id: str) -> bool:
        """风控决定是否已被签发或改单用过（一个决定只能用一次）。"""
        return (
            self._fetch(schema.ticket_issuance, {"decision_id": decision_id}) is not None
            or self._fetch(schema.order_amendments, {"decision_id": decision_id}) is not None
        )

    # ---------- 风控决定 ----------

    def add_risk_decision(self, decision: RiskDecision) -> bool:
        payload = decision.model_dump_json()
        return self._insert_once(
            schema.risk_decisions,
            {
                "decision_id": decision.decision_id,
                "intent_id": decision.intent_id,
                "verdict": decision.verdict.value,
                "decided_at": _iso(decision.decided_at),
                "payload": payload,
            },
            [{"decision_id": decision.decision_id}],
            lambda r: r.payload == payload,
        )

    def risk_decisions_for(self, intent_id: str) -> list[RiskDecision]:
        t = schema.risk_decisions
        stmt = (
            select(t.c.payload)
            .where(t.c.intent_id == intent_id)
            .order_by(t.c.decided_at, t.c.decision_id)
        )
        with self._reading("risk_decisions") as conn:
            return [RiskDecision.model_validate_json(p) for (p,) in conn.execute(stmt)]

    # ---------- 命令日志（request_id 去重） ----------

    def command_result(self, request_id: str) -> CommandRecord | None:
        """已执行命令的记录（S-01a：执行前先查，避免重复执行）。"""
        row = self._fetch(schema.command_log, {"request_id": request_id})
        if row is None:
            return None
        return CommandRecord(row.request_id, row.command, row.result, row.recorded_at)

    def record_command(
        self, request_id: str, command: str, result: str, at: datetime
    ) -> CommandRecord:
        """同一 request_id 只执行一次：已存在则返回原结果；命令不同则冲突。

        command 应是“命令名 + 规范化参数”的指纹（S-02 定义），
        这样同一 request_id 换了参数会被判为冲突。
        """
        record = CommandRecord(request_id, command, result, _iso(at))
        inserted = self._insert_once(
            schema.command_log,
            {
                "request_id": request_id,
                "command": command,
                "result": result,
                "recorded_at": record.recorded_at,
            },
            [{"request_id": request_id}],
            lambda r: r.command == command,
        )
        if inserted:
            return record
        row = self._fetch(schema.command_log, {"request_id": request_id})
        if row is None:  # pragma: no cover - 只追加，不会消失
            raise StorageError(f"命令 {request_id} 记录丢失")
        return CommandRecord(row.request_id, row.command, row.result, row.recorded_at)

    # ---------- 审计 ----------

    def append_audit(self, event: AuditEvent) -> bool:
        payload = event.model_dump_json()
        return self._insert_once(
            schema.audit_events,
            {
                "event_id": event.event_id,
                "occurred_at": _iso(event.occurred_at),
                "actor": event.actor,
                "action": event.action,
                "correlation_id": event.correlation_id,
                "payload": payload,
            },
            [{"event_id": event.event_id}],
            lambda r: r.payload == payload,
        )

    def latest_audit(self, action: str) -> AuditEvent | None:
        """某类审计事件的最新一条（按写入顺序）。"""
        t = schema.audit_events
        stmt = select(t.c.payload).where(t.c.action == action).order_by(t.c.seq.desc()).limit(1)
        with self._reading("audit_events") as conn:
            row = conn.execute(stmt).first()
            return None if row is None else AuditEvent.model_validate_json(row.payload)

    def audits(self, action: str) -> list[AuditEvent]:
        """某类审计事件，按写入顺序从新到旧。"""
        t = schema.audit_events
        stmt = select(t.c.payload).where(t.c.action == action).order_by(t.c.seq.desc())
        with self._reading("audit_events") as conn:
            return [AuditEvent.model_validate_json(p) for (p,) in conn.execute(stmt)]

    def audits_among(self, actions: Sequence[str]) -> list[AuditEvent]:
        """几类审计事件合在一起，按写入顺序从新到旧（不依赖时间戳先后）。"""
        t = schema.audit_events
        stmt = select(t.c.payload).where(t.c.action.in_(actions)).order_by(t.c.seq.desc())
        with self._reading("audit_events") as conn:
            return [AuditEvent.model_validate_json(p) for (p,) in conn.execute(stmt)]

    # ---------- 模式与急停 ----------

    def commit_mode_and_audits(
        self,
        audits: Sequence[AuditEvent],
        change: tuple[str, Mode, HaltState, datetime, str] | None = None,
    ) -> None:
        """在同一个事务里写入（可选的）模式变更与若干审计事件：要么全部落盘，要么全部不落盘。

        change 为（change_id, mode, halt, at, actor）。供 ModeManager 使用，
        避免“审计已写、HALT 未写”这类中间状态（C-04 评审 B1、I8）。
        任何键重复或数据库错误都抛 StorageError，整体回滚。
        """
        try:
            with self._engine.begin() as conn:
                if change is not None:
                    change_id, mode, halt, at, actor = change
                    conn.execute(
                        schema.mode_changes.insert().values(
                            change_id=change_id,
                            mode=Mode(mode).value,
                            halt=halt.model_dump_json(),
                            changed_at=_iso(at),
                            actor=actor,
                        )
                    )
                for event in audits:
                    conn.execute(
                        schema.audit_events.insert().values(
                            event_id=event.event_id,
                            occurred_at=_iso(event.occurred_at),
                            actor=event.actor,
                            action=event.action,
                            correlation_id=event.correlation_id,
                            payload=event.model_dump_json(),
                        )
                    )
        except DBAPIError as exc:
            raise StorageError(f"写入模式与审计失败，已整体回滚：{exc.orig}") from exc

    def append_mode_change(
        self, change_id: str, mode: Mode, halt: HaltState, at: datetime, actor: str
    ) -> bool:
        """记录一次模式变更；change_id 是幂等键。模式切换的校验在 ModeManager（C-04）。"""
        halt_json = halt.model_dump_json()
        mode_value = Mode(mode).value
        return self._insert_once(
            schema.mode_changes,
            {
                "change_id": change_id,
                "mode": mode_value,
                "halt": halt_json,
                "changed_at": _iso(at),
                "actor": actor,
            },
            [{"change_id": change_id}],
            lambda r: r.mode == mode_value and r.halt == halt_json and r.actor == actor,
        )

    def latest_mode(self) -> ModeChange | None:
        t = schema.mode_changes
        stmt = select(t).order_by(t.c.seq.desc()).limit(1)
        with self._reading("mode_changes") as conn:
            row = conn.execute(stmt).first()
            if row is None:
                return None
            return ModeChange(
                row.change_id,
                Mode(row.mode),
                HaltState.model_validate_json(row.halt),
                row.changed_at,
                row.actor,
            )
