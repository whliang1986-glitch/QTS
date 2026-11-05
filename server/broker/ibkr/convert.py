"""ib_async 对象 → BrokerAdapter 统一类型（B-03b，只读部分）。

纯函数，按属性名读取（鸭子类型），测试用 ib_async 自己的数据类即可，不需要 TWS。**未在模拟盘实测**：
字段含义与取值以 IBKR 文档与 ib_async 源码为准，B-03b 联调时逐条核对（核对前只读，不据此下单）。

失败即关闭：
- ib_async 用 UNSET_DOUBLE（约 1.8e308）与 NaN 表示“没有值”：
  数量、价格遇到它们一律拒绝（MappingError）；价格可以为空的地方（市价单、平均成本）返回 None；
- 时间必须带时区，统一转 UTC；
- 认不出的订单状态、方向抛 MappingError，订单类型认不出归为 OTHER（撤单需要确认）；
- 订单号：IBKR 的 orderId 只在同一个 clientId 内唯一。不是本连接下的单（别的 clientId、TWS 手工单）
  一律用 `perm-<permId>`；permId 也没有就拒绝（不能让两张单撞成同一个号）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from server.broker.base import (
    BrokerExecution,
    BrokerOrderState,
    BrokerOrderType,
    OrderRecord,
    OrderSource,
)
from server.broker.ibkr.mapping import PENDING_STATUSES, MappingError, order_state, order_type
from server.domain.account import CashBalance, Position
from server.domain.enums import Side

UNSET_DOUBLE_LIMIT = 1e300  # ib_async 的 UNSET_DOUBLE 是 sys.float_info.max：远大于任何真实值
AVERAGE_COST_PLACES = Decimal(
    "1e-10"
)  # 平均成本含佣金摊销，常有十几位小数：只是参考值，按 10 位取整
_SIDES = {"BUY": Side.BUY, "BOT": Side.BUY, "SELL": Side.SELL, "SLD": Side.SELL}
_CASH_TAGS = ("TotalCashValue", "SettledCash")
_VERIFY_TAGS = frozenset({"AccountType", "BaseCurrency", "Currency"})  # verify_account 用到的标签


def decimal_of(value: Any, what: str, *, allow_unset: bool = False) -> Decimal | None:
    """数值 → Decimal。float 先转字符串，避免二进制误差带进账本。"""
    if value is None:
        if allow_unset:
            return None
        raise MappingError(f"{what} 缺失")
    if isinstance(value, bool):
        raise MappingError(f"{what} 不是数值")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or abs(value) >= UNSET_DOUBLE_LIMIT:
            if allow_unset:
                return None
            raise MappingError(f"{what} 没有有效值（{value}）")
        return Decimal(repr(value))
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MappingError(f"{what} 不是数值：{value!r}") from exc
    if not result.is_finite():
        raise MappingError(f"{what} 不是有限值")
    return result


def _required(value: Any, what: str) -> Decimal:
    result = decimal_of(value, what)
    if result is None:  # pragma: no cover - allow_unset=False 时不会是 None
        raise MappingError(f"{what} 缺失")
    return result


def utc(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MappingError(f"{what} 不是带时区的时间")
    return value.astimezone(UTC)


def side_of(raw: str) -> Side:
    try:
        return _SIDES[str(raw).upper()]
    except KeyError:
        raise MappingError(f"认不出的买卖方向：{raw!r}") from None


def _symbol(contract: Any) -> str:
    symbol = str(getattr(contract, "symbol", "") or "").strip()
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    if not symbol:
        raise MappingError("合约没有代码")
    if not sec_type:  # 品种不明：不当作股票放行
        raise MappingError(f"{symbol} 的合约没有品种信息")
    if sec_type not in {"STK", "ETF"}:
        # 首期只有股票与 ETF 现货：其他品种（期权、期货等）照样上报，但标明品种与合约编号，
        # 交给对账当作差异（不同期权不能合并成同一个代码）
        con_id = int(getattr(contract, "conId", 0) or 0)
        return f"{symbol}:{sec_type}:{con_id}"
    return symbol


# ---------- 账户 ----------


def cash_balance(values: Sequence[Any], account_id: str, as_of: datetime) -> CashBalance:
    """账户摘要 → 现金。已结算现金来自 SettledCash，未结算 = TotalCashValue − SettledCash。

    两个标签都必须恰好有一条 USD 取值；缺失、多币种冲突、为负、已结算大于总现金都拒绝
    （ADR-15：现金账户只能用已结算现金买入，拿不准就不给）。字段含义未实测。
    """
    found: dict[str, Decimal] = {}
    for tag in _CASH_TAGS:
        rows = [v for v in values if getattr(v, "account", account_id) == account_id
                and str(v.tag) == tag]  # fmt: skip
        usd = [v for v in rows if str(getattr(v, "currency", "")).upper() == "USD"]
        if len(usd) != 1:
            currencies = sorted({str(getattr(v, "currency", "")) for v in rows})
            raise MappingError(f"{tag} 没有唯一的 USD 取值（币种：{currencies or '无'}）")
        found[tag] = _required(usd[0].value, tag)
    total, settled = found["TotalCashValue"], found["SettledCash"]
    if total < 0 or settled < 0 or settled > total:
        raise MappingError(f"现金数据不一致：总现金 {total}，已结算 {settled}")
    return CashBalance(
        account_id=account_id, currency="USD", settled=settled, unsettled=total - settled,
        as_of=utc(as_of, "as_of"),
    )  # fmt: skip


def summary_map(values: Sequence[Any], account_id: str) -> dict[str, str]:
    """账户摘要 → 标签 → 取值，供账户核验使用（AccountType、Currency 等非金额标签）。

    同一标签出现不同的取值就拒绝，不静默取其中一个。
    """
    result: dict[str, str] = {}
    for item in values:
        if getattr(item, "account", account_id) != account_id:
            continue
        tag, value = str(item.tag), str(item.value)
        if tag not in _VERIFY_TAGS:
            continue  # 只取核验用到的标签：别的标签（如同时有 USD 与 BASE 的金额）不影响核验
        if tag in result and result[tag] != value:
            raise MappingError(f"账户摘要的 {tag} 有不同取值")
        result[tag] = value
    return result


def position(item: Any, as_of: datetime) -> Position:
    cost = decimal_of(getattr(item, "avgCost", None), "平均成本", allow_unset=True)
    if cost is not None:
        cost = cost.quantize(AVERAGE_COST_PLACES, rounding=ROUND_HALF_EVEN)
    return Position(
        account_id=str(item.account), symbol=_symbol(item.contract),
        quantity=_required(item.position, "持仓数量"),
        average_cost=None if cost is None or cost <= 0 else cost,
        as_of=utc(as_of, "as_of"),
    )  # fmt: skip


# ---------- 订单 ----------


def _order_kind(order: Any) -> BrokerOrderType:
    """订单类型；带条件或可调整止损的一律按 OTHER（可能起保护作用，撤单需要确认）。"""
    adjusted = str(getattr(order, "adjustedOrderType", "") or "")
    if getattr(order, "conditions", None) or adjusted not in {"", "None"}:
        return BrokerOrderType.OTHER
    return order_type(str(order.orderType))


class PendingOrderError(MappingError):
    """订单还没有被券商完整确认（状态或数量还没到）：快照失败、稍后重查；事件路径不产生事件。"""


_TERMINAL = frozenset({BrokerOrderState.FILLED, BrokerOrderState.CANCELLED,
                       BrokerOrderState.REJECTED, BrokerOrderState.EXPIRED})  # fmt: skip


def broker_order_id(order_id: int, client_id: int, perm_id: int, own_client_id: int) -> str:
    """本连接下的单用 orderId；别的 clientId、TWS 手工单用 perm-<permId>；都没有就拒绝。"""
    if order_id > 0 and client_id == own_client_id:
        return str(order_id)
    if perm_id > 0:
        return f"perm-{perm_id}"
    raise MappingError("订单既不属于本连接、也没有 permId：无法给出唯一的订单号")


def _price(order: Any, kind: BrokerOrderType) -> Decimal | None:
    raw = str(order.orderType).upper()
    if raw.startswith("TRAIL"):
        return None  # 追踪止损的 auxPrice 是追踪幅度，不是价格
    value = order.auxPrice if kind is BrokerOrderType.STOP else order.lmtPrice
    price = decimal_of(value, "订单价格", allow_unset=True)
    return None if price is None or price <= 0 else price


def order_record(trade: Any, source: OrderSource, own_client_id: int) -> OrderRecord:
    """ib_async 的 Trade → OrderRecord。

    - 已完成订单：ib_async 只给出状态，成交量记在 order.filledQuantity 上（需联调核对）；
    - 挂单：用 orderStatus 的成交量与剩余量；状态还没到（空）或仍是中间状态时拒绝，
      调用方稍后重查（快照里不能漏掉还没被确认的单，风控要算它占用的资金）。
    """
    order, status, contract = trade.order, trade.orderStatus, trade.contract
    total = _required(order.totalQuantity, "订单数量")
    raw_status = str(status.status or "")
    if not raw_status or raw_status in PENDING_STATUSES:
        raise PendingOrderError(f"订单状态还没有确认（{raw_status or '空'}），稍后重查")
    if source is OrderSource.COMPLETED:
        filled = _required(getattr(order, "filledQuantity", None), "已完成订单的成交量")
        remaining = total - filled
        if remaining < 0:  # 部分成交后撤单时，剩余量就是没有成交的部分
            raise MappingError(f"成交量 {filled} 大于订单数量 {total}")
    else:
        filled = _required(status.filled, "成交量")
        remaining = _required(status.remaining, "剩余量")
    state = order_state(raw_status, filled, remaining)
    if state is None:  # pragma: no cover - 中间状态已在上面拒绝
        raise PendingOrderError("订单状态还没有确认")
    # 挂单：ib_async 收到 openOrder 时就建好 Trade，成交量与剩余量要等后面的 orderStatus 才更新
    # （之前都是默认的 0）：数量对不上订单总数、或未结束却没有剩余，都按“还没确认”处理
    unconfirmed = filled + remaining != total or (state not in _TERMINAL and remaining <= 0)
    if source is OrderSource.OPEN and unconfirmed:
        detail = f"总数 {total}，成交 {filled}，剩余 {remaining}"
        if state in _TERMINAL:  # 终态之后不会再有回报：不能静默等待，按不可信上报、重新对账
            raise MappingError(f"订单已结束但数量对不上（{detail}）")
        raise PendingOrderError(f"订单数量还没有确认（{detail}），稍后重查")
    kind = _order_kind(order)
    perm = int(getattr(order, "permId", 0) or 0)
    return OrderRecord(
        broker_order_id=broker_order_id(int(getattr(order, "orderId", 0) or 0),
                                        int(getattr(order, "clientId", 0) or 0), perm,
                                        own_client_id),
        order_ref=str(getattr(order, "orderRef", "") or "") or None,
        perm_id=str(perm) if perm else None,
        account_id=str(order.account), symbol=_symbol(contract), side=side_of(order.action),
        order_type=kind, raw_order_type=str(order.orderType), price=_price(order, kind),
        time_in_force=str(order.tif or ""), total_quantity=total,
        filled_quantity=filled, remaining_quantity=remaining, state=state, source=source,
    )  # fmt: skip


def order_records(
    trades: Sequence[Any], source: OrderSource, own_client_id: int
) -> list[OrderRecord]:
    return [order_record(t, source, own_client_id) for t in trades]


def is_terminal(record: OrderRecord) -> bool:
    return record.state in _TERMINAL


# ---------- 成交 ----------


def execution(fill: Any, own_client_id: int) -> BrokerExecution:
    ex = fill.execution
    report = getattr(fill, "commissionReport", None)
    commission = None
    # ib_async 在佣金回报到来前用空的 CommissionReport 占位（execId 为空、佣金 0）：当作“还不知道”，
    # 不能记成 0（佣金以后到的 CommissionReport 为准，base.BrokerExecution）。
    # 佣金回报必须对得上这笔成交，币种必须是 USD
    report_id = str(getattr(report, "execId", None) or "") if report is not None else ""
    if report_id:
        if report_id != str(ex.execId):
            raise MappingError("佣金回报与成交编号对不上")
        currency = str(getattr(report, "currency", "") or "").upper()  # 为空也拒绝
        if currency != "USD":
            raise MappingError(f"佣金币种不是 USD（{currency}）")
        commission = decimal_of(getattr(report, "commission", None), "佣金", allow_unset=True)
    perm = int(getattr(ex, "permId", 0) or 0)
    order_id = int(getattr(ex, "orderId", 0) or 0)
    client_id = int(getattr(ex, "clientId", 0) or 0)
    mine = order_id > 0 and client_id == own_client_id
    return BrokerExecution(
        execution_id=str(ex.execId), order_ref=str(getattr(ex, "orderRef", "") or "") or None,
        broker_order_id=str(order_id) if mine else (f"perm-{perm}" if perm else None),
        perm_id=str(perm) if perm else None, account_id=str(ex.acctNumber),
        symbol=_symbol(fill.contract), side=side_of(ex.side),
        quantity=_required(ex.shares, "成交数量"), price=_required(ex.price, "成交价格"),
        commission=commission, executed_at=utc(ex.time, "成交时间"),
    )  # fmt: skip


__all__ = (
    "AVERAGE_COST_PLACES",
    "UNSET_DOUBLE_LIMIT",
    "PendingOrderError",
    "broker_order_id",
    "cash_balance",
    "decimal_of",
    "execution",
    "is_terminal",
    "order_record",
    "order_records",
    "position",
    "side_of",
    "summary_map",
    "utc",
)
