"""保护性止损覆盖核对（R-06a，V4.1 第 7 章“每个持仓对应一张挂在券商端的保护性止损单”）。

纯函数，按**券商事实**逐标的核对：每个多头持仓都要有、且只有一张卖出止损单，数量等于持仓。
结果单独计数，交给对账报告与告警（P0 `protective_stop_missing`）：

| 问题 | 含义 | 处理 |
|---|---|---|
| MISSING | 有持仓，没有卖出止损单 | P0：持仓没有保护 |
| QUANTITY_MISMATCH | 止损数量与持仓不相等（少了保护不足，多了会超卖） | P0 |
| MULTIPLE | 同一标的有多张卖出止损单（合计可能超卖） | P0 |
| ORPHAN | 没有持仓却挂着卖出止损单（触发即超卖、做空） | P0 |
| FRACTIONAL | 持仓不是整数股，止损数量不可能等于持仓 | P0，交给人工 |
| UNRECOGNISED | 有不算覆盖的卖出单（止损限价、追踪、带条件、非 GTC 止损） | P0，人工核对 |
| OVERSELL | 卖出单合计超过持仓（止损加减仓，或没持仓挂卖单） | P0 |
| NEGATIVE | 负持仓 | 交给对账（R-07 已进入 SAFE HALT），这里只记一条 |

不在这里做的：
- 止损价是否合规（R-04 规则在下单前检查）；
- 已跌破止损线时怎么处理（由 Dylan 决定，见 R-06 任务单）；
- 创建、同步止损单（R-06b）。

失败即关闭：
- 取不到持仓或挂单时，调用方不得调用本函数（空快照会得出“全部覆盖”）。R-06b 接入时用测试锁住这一点；
- 已结束的订单、负的或非有限的数量、别的账户的数据，都直接拒绝（入参违约），不静默跳过；
- 只按代码归并：首期只有美元计价的股票与 ETF，同一代码对应多个合约的情况不考虑（R-06b 联调时核对）。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from server.domain.enums import Side


class Coverage(StrEnum):
    MISSING = "MISSING"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    MULTIPLE = "MULTIPLE"
    ORPHAN = "ORPHAN"
    FRACTIONAL = "FRACTIONAL"
    UNRECOGNISED = "UNRECOGNISED"
    OVERSELL = "OVERSELL"
    NEGATIVE = "NEGATIVE"


class HeldPosition(Protocol):
    @property
    def account_id(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def quantity(self) -> Decimal: ...


class WorkingOrder(Protocol):
    """券商侧订单的最小视图，由调用方从券商记录转换而来（risk 层不 import broker）。

    转换口径（失败即关闭）：
    - `kind`：只有普通止损单（IBKR 的 STP）填 "STOP"，普通限价单填 "LIMIT"；
      止损限价（STP LMT）、追踪止损（TRAIL、TRAIL LIMIT）、带条件或可调整的订单一律填 "OTHER"
      （跳空时可能不成交，不一定起保护作用）；
    - `working`：订单是否仍在券商侧有效。只传有效订单；传入已结束的订单是调用方的错误，直接拒绝；
    - `time_in_force`：原样填 "DAY"、"GTD"、"GTC"……只有 GTC 的止损单才算覆盖（当日单收盘即失效）。
    """

    @property
    def account_id(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def side(self) -> Side: ...

    @property
    def remaining_quantity(self) -> Decimal: ...

    @property
    def kind(self) -> str: ...

    @property
    def working(self) -> bool: ...

    @property
    def time_in_force(self) -> str: ...


@dataclass(frozen=True)
class Gap:
    symbol: str
    coverage: Coverage
    position: Decimal
    stop_quantity: Decimal
    detail: str


@dataclass(frozen=True)
class CoverageReport:
    account_id: str
    covered: tuple[str, ...]  # 恰好一张卖出止损、数量等于持仓的标的
    gaps: tuple[Gap, ...]

    @property
    def clean(self) -> bool:
        return not self.gaps

    def alert_symbols(self) -> tuple[str, ...]:
        """需要发 P0 告警的标的（负持仓除外：对账已经处理）。"""
        return tuple(sorted({g.symbol for g in self.gaps if g.coverage is not Coverage.NEGATIVE}))


def check_coverage(
    account_id: str,
    positions: Iterable[HeldPosition],
    orders: Iterable[WorkingOrder],
) -> CoverageReport:
    """逐标的核对保护性止损覆盖。只看登记账户的数据；别的账户出现时直接拒绝（不静默过滤）。"""
    held: dict[str, Decimal] = defaultdict(Decimal)
    for p in positions:
        _same_account(account_id, p.account_id, "持仓")
        held[p.symbol] += _finite(p.quantity, f"{p.symbol} 持仓")
    sells: dict[str, _Sells] = defaultdict(_Sells)
    for o in orders:
        _same_account(account_id, o.account_id, "订单")
        if o.working is not True:
            raise ValueError(f"保护性止损核对收到已结束的订单（{o.symbol}）：只能传有效订单")
        remaining = _finite(o.remaining_quantity, f"{o.symbol} 订单剩余量")
        if remaining < 0:
            raise ValueError(f"{o.symbol} 订单剩余量为负")
        if o.side is not Side.SELL or remaining == 0:
            continue
        bucket = sells[o.symbol]
        if o.kind == "STOP" and o.time_in_force == "GTC":
            bucket.stops.append(remaining)
        elif o.kind == "LIMIT":
            bucket.limits += remaining  # 减仓单：不算保护，但计入卖出合计（超卖检查）
        else:  # 止损限价、追踪、带条件、当日止损……：不算覆盖，交给人工
            bucket.unknown += 1
    covered: list[str] = []
    gaps: list[Gap] = []
    for symbol in sorted(set(held) | set(sells)):
        position = held.get(symbol, Decimal(0))
        gap = _check_symbol(symbol, position, sells.get(symbol, _Sells()))
        if gap is not None:
            gaps.append(gap)
        elif position > 0:
            covered.append(symbol)
    return CoverageReport(account_id=account_id, covered=tuple(covered), gaps=tuple(gaps))


@dataclass
class _Sells:
    stops: list[Decimal] = field(default_factory=list)  # GTC 普通止损单的剩余量
    limits: Decimal = Decimal(0)  # 卖出限价单剩余量合计
    unknown: int = 0  # 认不出、不算覆盖的卖出单张数


def _finite(value: Decimal, what: str) -> Decimal:
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"{what} 不是有限数值")
    return number


def _check_symbol(  # noqa: PLR0911 - 每种问题一个返回，逐条列出便于审计
    symbol: str, position: Decimal, sells: _Sells
) -> Gap | None:
    stops = sells.stops
    total = sum(stops, Decimal(0))

    def gap(coverage: Coverage, detail: str) -> Gap:
        if sells.limits > 0 and coverage is not Coverage.OVERSELL:  # 超卖信息不被别的问题盖住
            detail += f"；另有卖出限价单 {sells.limits} 股"
        return Gap(symbol, coverage, position, total, detail)

    if position < 0:
        return gap(Coverage.NEGATIVE, "负持仓：由对账进入 SAFE HALT")
    if sells.unknown:
        return gap(Coverage.UNRECOGNISED, f"有 {sells.unknown} 张不算覆盖的卖出单（止损限价、"
                   "追踪、带条件或非 GTC 止损），需人工核对")  # fmt: skip
    if position == 0:
        if stops:
            return gap(Coverage.ORPHAN, f"没有持仓却挂着 {len(stops)} 张卖出止损单")
        if sells.limits > 0:
            return gap(Coverage.OVERSELL, f"没有持仓却挂着卖出限价单 {sells.limits} 股")
        return None
    if position != position.to_integral_value():
        return gap(Coverage.FRACTIONAL, "持仓不是整数股，止损数量无法等于持仓")
    if not stops:
        return gap(Coverage.MISSING, "持仓没有保护性止损")
    if len(stops) > 1:
        return gap(Coverage.MULTIPLE, f"有 {len(stops)} 张卖出止损单")
    if total != position:
        return gap(Coverage.QUANTITY_MISMATCH, f"止损数量 {total} 与持仓 {position} 不相等")
    if sells.limits > 0:  # 止损与减仓同时成交会卖出超过持仓（R-06b 先撤止损再减仓）
        return gap(Coverage.OVERSELL, f"止损 {total} 加卖出限价 {sells.limits} 超过持仓 {position}")
    return None


def _same_account(expected: str, actual: str, what: str) -> None:
    if actual != expected:
        raise ValueError(f"保护性止损核对收到不属于登记账户的{what}")


__all__ = ("Coverage", "CoverageReport", "Gap", "HeldPosition", "WorkingOrder", "check_coverage")
