"""撮合内核（B-02）：FakeBroker 与 T-04 回测的 SimBroker 共用。

纯计算：无 I/O、不读时钟，时间与结算日都由调用方传入。只记录券商侧会发生的事实，不做风控判断。

- `match_bar`：按一根 K 线撮合限价单或止损单；K 线开始早于下单时间即拒绝（防前视，V4.1 8.3）。
- 佣金模型：`NoCommission`、`FixedCommission`（IBKR 固定费率口径：每股、最低、成交额上限比例）。
- `Ledger`：持仓与加权成本、已结算 / 未结算现金（ADR-15）、拆股、分红、无回报的券商侧持仓变动。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Protocol

from server.domain.enums import Side
from server.domain.market import MarketBar

CENT = Decimal("0.01")


# ---------- 撮合 ----------


@dataclass(frozen=True)
class RestingOrder:
    """待撮合的挂单。stop 为真时 price 是止损触发价，否则是限价。"""

    side: Side
    stop: bool
    price: Decimal
    remaining: Decimal
    submitted_at: datetime

    def __post_init__(self) -> None:
        if self.remaining <= 0:
            raise ValueError("剩余数量必须为正")
        if self.price <= 0:
            raise ValueError("价格必须为正")


@dataclass(frozen=True)
class BarFill:
    quantity: Decimal
    price: Decimal


def match_bar(
    order: RestingOrder, bar: MarketBar, participation: Decimal | None = None
) -> BarFill | None:
    """限价单：买在 low ≤ 限价时按 min(开盘, 限价) 成交，卖对称。

    止损单（触发后按市价）：卖在 low ≤ 止损价时按 min(开盘, 止损价) 成交
    （跳空按更差的开盘价），买对称。
    participation 为 K 线成交量的参与率上限（0, 1]，成交量向下取整到整股，为 0 则不成交。
    """
    if participation is not None and not (0 < participation <= 1):
        raise ValueError("参与率必须在 (0, 1] 内")
    if bar.start < order.submitted_at:
        raise ValueError("K 线开始早于下单时间：不得用下单前的行情撮合（前视）")
    price, remaining = order.price, order.remaining
    buy = order.side is Side.BUY
    if order.stop:
        triggered = bar.high >= price if buy else bar.low <= price
        fill_price = max(bar.open, price) if buy else min(bar.open, price)
    else:
        triggered = bar.low <= price if buy else bar.high >= price
        fill_price = min(bar.open, price) if buy else max(bar.open, price)
    if not triggered:
        return None
    quantity = remaining
    if participation is not None:
        cap = (bar.volume * participation).to_integral_value(rounding=ROUND_DOWN)
        quantity = min(remaining, cap)
    if quantity <= 0:
        return None
    return BarFill(quantity=quantity, price=fill_price)


# ---------- 佣金 ----------


class CommissionModel(Protocol):
    def __call__(self, quantity: Decimal, price: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class NoCommission:
    def __call__(self, quantity: Decimal, price: Decimal) -> Decimal:
        return Decimal(0)


@dataclass(frozen=True)
class FixedCommission:
    """IBKR 美股固定费率口径（不含监管费与交易所费，后续按实际账单校准）。"""

    per_share: Decimal = Decimal("0.005")
    minimum: Decimal = Decimal("1.00")
    max_rate: Decimal = Decimal("0.01")  # 不超过成交额的 1%

    def __call__(self, quantity: Decimal, price: Decimal) -> Decimal:
        raw = max(quantity * self.per_share, self.minimum)
        raw = min(raw, quantity * price * self.max_rate)
        return raw.quantize(CENT, rounding=ROUND_HALF_UP)


# ---------- 账本 ----------


@dataclass(frozen=True)
class Trade:
    """一笔记入账本的成交。settle_on 为结算日（美股 T+1，由调用方按交易日历给出）。"""

    side: Side
    symbol: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    settle_on: date

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.price <= 0 or self.commission < 0:
            raise ValueError("成交数量、价格必须为正，佣金不得为负")


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: Decimal
    average_cost: Decimal | None  # 空仓或负持仓时为空


@dataclass
class _Lot:
    quantity: Decimal
    cost: Decimal | None


class Ledger:
    """券商侧账本。买入先扣已结算现金，不足再扣最早结算的未结算款；卖出款按结算日进入未结算。"""

    def __init__(
        self, settled: Decimal, positions: dict[str, tuple[Decimal, Decimal]] | None = None
    ) -> None:
        self.settled = settled
        self._lots = {s: _Lot(q, c) for s, (q, c) in (positions or {}).items()}
        self._pending: list[tuple[date, Decimal]] = []

    # ---------- 查询 ----------

    @property
    def unsettled(self) -> Decimal:
        return sum((amount for _, amount in self._pending), Decimal(0))

    def buying_power(self, *, include_unsettled: bool) -> Decimal:
        """可用于买入的现金。

        现金账户能否用未结算卖出款买入需 B-05 实测，调用方默认按保守口径只用已结算。
        """
        return self.settled + self.unsettled if include_unsettled else self.settled

    def position(self, symbol: str) -> Holding:
        lot = self._lots.get(symbol, _Lot(Decimal(0), None))
        return Holding(symbol=symbol, quantity=lot.quantity, average_cost=lot.cost)

    def positions(self) -> list[Holding]:
        return [self.position(s) for s in sorted(self._lots) if self._lots[s].quantity != 0]

    # ---------- 成交 ----------

    def apply_fill(self, trade: Trade) -> None:
        side, symbol, quantity, price = trade.side, trade.symbol, trade.quantity, trade.price
        commission, settle_on = trade.commission, trade.settle_on
        notional = quantity * price
        self.settled -= commission  # 佣金立即扣已结算现金
        lot = self._lots.setdefault(symbol, _Lot(Decimal(0), None))
        if side is Side.BUY:
            self._debit(notional)
            if lot.quantity > 0 and lot.cost is None:  # 成本未知的持仓加仓：成本仍未知，不当作 0
                lot.quantity += quantity
            elif lot.quantity >= 0:
                held = lot.quantity * (lot.cost or Decimal(0))
                lot.quantity += quantity
                lot.cost = (held + notional) / lot.quantity
            else:  # 回补负持仓：回到正数部分按本次成交价计成本
                lot.quantity += quantity
                lot.cost = price if lot.quantity > 0 else None
        else:
            self._pending.append((settle_on, notional))
            lot.quantity -= quantity
            if lot.quantity <= 0:
                lot.cost = None
        if lot.quantity == 0:
            lot.cost = None

    def correct_fill(self, old: Trade, new: Trade) -> None:
        """成交更正：按差额调整数量、现金与成本。

        结果等同于当初就按 new 入账（中间没有其他成交时精确）。
        """
        if (old.side, old.symbol, old.settle_on) != (new.side, new.symbol, new.settle_on):
            raise ValueError("成交更正只能改数量、价格与佣金")
        self.settled += old.commission - new.commission
        lot = self._lots.setdefault(old.symbol, _Lot(Decimal(0), None))
        diff = new.quantity * new.price - old.quantity * old.price
        if old.side is Side.BUY:
            before = lot.quantity
            if diff > 0:
                self._debit(diff)
            else:
                self.settled -= diff  # 退款全部回到已结算（原扣款若来自未结算款，结算口径略有偏差）
            lot.quantity += new.quantity - old.quantity
            if lot.quantity <= 0:
                lot.cost = None
            elif lot.cost is not None and before > 0:
                total = before * lot.cost - old.quantity * old.price + new.quantity * new.price
                lot.cost = total / lot.quantity
        else:
            self._adjust_pending(new.settle_on, diff)
            lot.quantity -= new.quantity - old.quantity
            if lot.quantity <= 0:
                lot.cost = None

    def _adjust_pending(self, settle_on: date, diff: Decimal) -> None:
        """把差额记到同一结算日的未结算款；已结算或已被买入占用的部分记到已结算现金。"""
        for i, (day, amount) in enumerate(self._pending):
            if day == settle_on:
                adjusted = amount + diff
                self._pending[i] = (day, max(adjusted, Decimal(0)))
                self.settled += min(adjusted, Decimal(0))
                return
        self.settled += diff

    def _debit(self, amount: Decimal) -> None:
        from_settled = min(amount, max(self.settled, Decimal(0)))
        self.settled -= from_settled
        rest = amount - from_settled
        self._pending.sort(key=lambda p: p[0])
        kept: list[tuple[date, Decimal]] = []
        for settle_on, pending in self._pending:
            take = min(rest, pending)
            rest -= take
            if pending - take > 0:
                kept.append((settle_on, pending - take))
        self._pending = kept
        self.settled -= rest  # 仍不足时已结算现金为负（券商侧事实，由风控与对账处理）

    def settle(self, through: date) -> None:
        """结算日不晚于 through 的未结算款转为已结算。"""
        due = [amount for settle_on, amount in self._pending if settle_on <= through]
        self._pending = [(d, a) for d, a in self._pending if d > through]
        self.settled += sum(due, Decimal(0))

    # ---------- 公司行动与券商侧变动 ----------

    def apply_split(self, symbol: str, numerator: Decimal, denominator: Decimal) -> None:
        """numerator 拆 denominator（3:1 拆股为 3、1；1 合 4 为 1、4）。零股如实保留。"""
        if numerator <= 0 or denominator <= 0:
            raise ValueError("拆股比例必须为正")
        lot = self._lots.get(symbol)
        if lot is None:
            return
        lot.quantity = lot.quantity * numerator / denominator
        if lot.cost is not None:
            lot.cost = lot.cost * denominator / numerator

    def apply_dividend(self, symbol: str, per_share: Decimal) -> None:
        """现金分红（派息日）计入已结算现金，按分四舍五入。"""
        lot = self._lots.get(symbol)
        if lot is None or lot.quantity == 0:
            return
        self.settled += (lot.quantity * per_share).quantize(CENT, rounding=ROUND_HALF_UP)

    def adjust_position(self, symbol: str, delta: Decimal) -> None:
        """无成交回报的券商侧持仓变动（转仓、券商调整）：只改数量，供对账场景使用。"""
        lot = self._lots.setdefault(symbol, _Lot(Decimal(0), None))
        lot.quantity += delta
        if lot.quantity <= 0:
            lot.cost = None
