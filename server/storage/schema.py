"""账本表结构（C-03，ADR-05）。SQLAlchemy Core，不用 ORM。

所有账本表都只追加：订单、成交、风控决定、命令日志、审计、模式变更，以及状态变化都写成新行。
每张表有三个触发器（迁移 0001 建立，打开账本时逐一核对）：
- <表>_no_update / <表>_no_delete：禁止 UPDATE、DELETE
- <表>_no_replace：BEFORE INSERT 检查唯一键已存在即中止，堵住 INSERT OR REPLACE / REPLACE INTO
  （REPLACE 会先删后插，默认不触发 DELETE 触发器）；连接上另开 recursive_triggers 作为第二层
金额与数量存 Decimal 字符串，时间存定长 UTC ISO 字符串。
"""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table, Text

metadata = MetaData()

orders = Table(
    "orders",
    metadata,
    Column("intent_id", Text, primary_key=True),
    Column("account_id", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("order_class", Text, nullable=False),
    Column("created_at", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

broker_orders = Table(
    "broker_orders",
    metadata,
    Column("broker_order_id", Text, primary_key=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, unique=True),
    Column("order_class", Text, nullable=False),
    Column("submitted_at", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

perm_ids = Table(
    "perm_ids",
    metadata,
    Column("perm_id", Text, primary_key=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, unique=True),
    Column("recorded_at", Text, nullable=False),
)

order_events = Table(
    "order_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Text, nullable=False, unique=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, index=True),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("occurred_at", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

executions = Table(
    "executions",
    metadata,
    Column("execution_id", Text, primary_key=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, index=True),
    Column("broker_order_id", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("quantity", Text, nullable=False),
    Column("price", Text, nullable=False),
    Column("commission", Text, nullable=False),
    Column("executed_at", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

risk_decisions = Table(
    "risk_decisions",
    metadata,
    Column("decision_id", Text, primary_key=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, index=True),
    Column("verdict", Text, nullable=False),
    Column("decided_at", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

command_log = Table(
    "command_log",
    metadata,
    Column("request_id", Text, primary_key=True),
    Column("command", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Text, nullable=False, unique=True),
    Column("occurred_at", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

mode_changes = Table(
    "mode_changes",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("change_id", Text, nullable=False, unique=True),
    Column("mode", Text, nullable=False),
    Column("halt", Text, nullable=False),
    Column("changed_at", Text, nullable=False),
    Column("actor", Text, nullable=False),
)

# O-01（迁移 0003）：一次签发用掉一个风控决定；下单数量随签发记录
ticket_issuance = Table(
    "ticket_issuance",
    metadata,
    Column("broker_order_id", Text, ForeignKey("broker_orders.broker_order_id"), primary_key=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False, unique=True),
    Column(
        "decision_id",
        Text,
        ForeignKey("risk_decisions.decision_id"),
        nullable=False,
        unique=True,
    ),
    Column("quantity", Text, nullable=False),
    Column("issued_at", Text, nullable=False),
)

# O-01（迁移 0003）：无法关联到本系统订单的成交照实入账，不丢弃
unmatched_executions = Table(
    "unmatched_executions",
    metadata,
    Column("execution_id", Text, primary_key=True),
    Column("reason", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
    Column("payload", Text, nullable=False),
)

# O-02（迁移 0004）：成交更正——更正后的成交另起一行，这里记“谁被谁取代”
execution_supersessions = Table(
    "execution_supersessions",
    metadata,
    Column(
        "superseded_execution_id", Text, ForeignKey("executions.execution_id"), primary_key=True
    ),
    Column(
        "superseding_execution_id",
        Text,
        ForeignKey("executions.execution_id"),
        nullable=False,
        unique=True,
    ),
    Column("recorded_at", Text, nullable=False),
)

# O-03（迁移 0005）：改单请求先落账（决定唯一），券商调用后记录结果
order_amendments = Table(
    "order_amendments",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("amendment_id", Text, nullable=False, unique=True),
    Column("intent_id", Text, ForeignKey("orders.intent_id"), nullable=False),
    Column(
        "decision_id",
        Text,
        ForeignKey("risk_decisions.decision_id"),
        nullable=False,
        unique=True,
    ),
    Column("new_price", Text, nullable=False),
    Column("new_quantity", Text, nullable=False),
    Column("requested_at", Text, nullable=False),
)

# 改单结果是事件：最后一条为当前结果（UNKNOWN 可查证转正，ACCEPTED 可被异步拒绝）
order_amendment_results = Table(
    "order_amendment_results",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column(
        "amendment_id",
        Text,
        ForeignKey("order_amendments.amendment_id"),
        nullable=False,
        index=True,
    ),
    Column("result", Text, nullable=False),
    Column("recorded_at", Text, nullable=False),
)

LEDGER_TABLES = frozenset(metadata.tables)
# 每张账本表必须存在的触发器；打开账本时缺一个即拒绝（防止迁移重建表时触发器丢失）
LEDGER_TRIGGERS = frozenset(
    {f"{table}_no_{op}" for table in LEDGER_TABLES for op in ("update", "delete", "replace")}
    # 迁移 0002：G7 之前账本里不得出现实盘模式（解除需要 Dylan 批准的新迁移）
    | {"mode_changes_no_live"}
    # 迁移 0003：成交必须挂在同一意图的券商订单上
    | {"executions_no_foreign_order"}
    # 迁移 0004：更正只能在同一张订单内
    | {"execution_supersessions_no_cross_intent"}
    # 迁移 0005：改单决定必须新且属于同一意图；改单结果的取值与先后关系
    | {"order_amendments_no_cross_intent", "order_amendment_results_no_bad_result"}
)
