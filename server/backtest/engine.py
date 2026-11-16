"""回测引擎（T-04，V4.1 8.3）：复用 TradingCore、RiskEngine、OrderManager 与 SimBroker 撮合内核。

核心原则：
- **同路径**：回测里的 OMS、风控、策略运行器与模拟盘/实盘是同一份代码，通过 `build_backtest_core`
  在 `Mode.BACKTEST` 下运行（仅限临时账本 + FakeBroker/SimBroker + workerd 角色）。
- **时间语义（T-04b）**：信号可用时间（昨日及更早收盘 `bar.end <= decision_at`）
  < 下单时间（`decision_at`）< 成交时间（`[decision_at, session.close]`）。
  严禁偷看当日尚未收盘的 K 线，违者抛 `LookaheadError`。
- **可复现**：实验绑定数据版本、代码 commit、参数哈希、股票池与成本模型，
  输出净值曲线与成交明细 Parquet，绝不写生产 `ledger.db`。
- **独立复算（T-04d）**：提供最小独立向量化双动量复算函数，用于与回测引擎逐日对照持仓与扣费净值。
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from server.broker.fake.broker import FakeBroker, FakeBrokerServer
from server.broker.sim.kernel import CommissionModel, NoCommission
from server.data.calendar import Session, TradingCalendar
from server.domain.account import Account
from server.domain.enums import Mode, OrderClass, Side
from server.domain.instrument import Instrument
from server.domain.market import MarketBar, Quote
from server.domain.orders import Execution
from server.risk.assembly import CoreInputs, build_backtest_core
from server.risk.context import LedgerStopCooldownSource
from server.risk.engine import Outcome
from server.risk.mode_manager import ModeConfig
from server.risk.parameters import RiskParameters
from server.risk.reconcile import CorporateAction, ReconKind
from server.risk.run_state import Component, Kind
from server.storage.ledger import MAX_VIEW_ROWS, LedgerRepository, open_ledger
from server.storage.migrate import upgrade
from server.strategy.base import (
    Proposal,
    Strategy,
    StrategyContext,
    StrategyError,
    evaluate,
    identity_of,
)

BPS = Decimal("0.0001")
BACKTEST_COMPONENTS: tuple[Component, ...] = (
    Component("broker", Kind.ACCOUNT, critical=True, stale_after=timedelta(days=365)),
    Component("account_sync", Kind.ACCOUNT, critical=True, stale_after=timedelta(days=365)),
    Component("quotes", Kind.DATA, critical=True, stale_after=timedelta(days=365)),
)


class LookaheadError(ValueError):
    """回测时间语义违约或前视泄漏：使用了决策时刻尚未发布的数据，或成交早于下单。"""


class BacktestError(RuntimeError):
    """回测运行或对账失败。"""


class _ManualClock:
    def __init__(self, initial: datetime) -> None:
        self._now = initial

    def __call__(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("回测时钟必须带时区")
        if at < self._now:
            raise LookaheadError(f"回测时钟不允许倒流：{self._now} -> {at}")
        self._now = at.astimezone(UTC)


class _QuoteBoard:
    def __init__(self) -> None:
        self.current: dict[str, Quote] = {}

    async def quotes(self, symbols: Collection[str]) -> Mapping[str, Quote]:
        return {s: self.current[s] for s in symbols if s in self.current}


class _StaticCorporateActions:
    def __init__(self, events: Sequence[CorporateAction] = ()) -> None:
        self._events = tuple(events)

    async def actions(self, symbols: Collection[str]) -> Sequence[CorporateAction]:
        wanted = set(symbols)
        return [e for e in self._events if e.symbol in wanted]


@dataclass(frozen=True)
class BacktestConfig:
    experiment_id: str
    account: Account
    initial_cash: Decimal
    start: date
    end: date
    instruments: Mapping[str, Instrument]
    parameters: RiskParameters
    commission: CommissionModel = field(default_factory=NoCommission)
    participation: Decimal | None = None
    decision_delay: timedelta = timedelta(minutes=5)
    half_spread_bps: Decimal = Decimal(2)
    data_version: str = "unadjusted-v1"
    code_commit: str = "HEAD"
    params_hash: str = "default"
    cost_model_name: str = "no_commission"
    corporate_actions: Sequence[CorporateAction] = ()

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id 不能为空")
        if self.initial_cash <= Decimal(0):
            raise ValueError("initial_cash 必须大于 0")
        if self.start > self.end:
            raise ValueError("start 不能晚于 end")
        if self.decision_delay < timedelta(0):
            raise ValueError("decision_delay 不能为负")
        if self.half_spread_bps < Decimal(0):
            raise ValueError("half_spread_bps 不能为负")


@dataclass(frozen=True)
class EquityPoint:
    day: date
    at: datetime
    settled_cash: Decimal
    unsettled_cash: Decimal
    positions_value: Decimal
    nav: Decimal
    positions: Mapping[str, Decimal]
    cumulative_commission: Decimal


@dataclass(frozen=True)
class BacktestResult:
    experiment_id: str
    manifest: Mapping[str, Any]
    equity_curve: tuple[EquityPoint, ...]
    executions: tuple[Execution, ...]
    outcomes: tuple[Outcome, ...]

    def write_parquet(self, backtests_root: Path) -> Path:
        """将净值曲线、成交明细与实验元数据写入 backtests_root/<experiment_id>/。"""
        out_dir = backtests_root / self.experiment_id
        if out_dir.exists():
            raise FileExistsError(f"回测输出目录已存在，拒绝覆盖：{out_dir}")
        out_dir.mkdir(parents=True, exist_ok=False)
        eq_table = pa.table(
            {
                "day": [p.day.isoformat() for p in self.equity_curve],
                "at": [p.at.isoformat() for p in self.equity_curve],
                "settled_cash": [str(p.settled_cash) for p in self.equity_curve],
                "unsettled_cash": [str(p.unsettled_cash) for p in self.equity_curve],
                "positions_value": [str(p.positions_value) for p in self.equity_curve],
                "nav": [str(p.nav) for p in self.equity_curve],
                "cumulative_commission": [str(p.cumulative_commission) for p in self.equity_curve],
                "positions_json": [
                    json.dumps({k: str(v) for k, v in sorted(p.positions.items())})
                    for p in self.equity_curve
                ],
            }
        )
        ex_table = pa.table(
            {
                "execution_id": [e.execution_id for e in self.executions],
                "intent_id": [e.intent_id for e in self.executions],
                "broker_order_id": [e.broker_order_id for e in self.executions],
                "symbol": [e.symbol for e in self.executions],
                "side": [e.side.value for e in self.executions],
                "quantity": [str(e.quantity) for e in self.executions],
                "price": [str(e.price) for e in self.executions],
                "commission": [str(e.commission) for e in self.executions],
                "executed_at": [e.executed_at.isoformat() for e in self.executions],
            }
        )
        pq.write_table(eq_table, out_dir / "equity_curve.parquet")
        pq.write_table(ex_table, out_dir / "executions.parquet")
        (out_dir / "manifest.json").write_text(
            json.dumps(dict(self.manifest), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return out_dir


def _next_session_day(calendar: TradingCalendar, executed_at: datetime) -> date:
    """按交易日历计算 T+1 结算日；若超出日历末尾则回退到自然日 +1。"""
    try:
        return calendar.next_session(executed_at).day
    except Exception:
        return executed_at.date() + timedelta(days=1)


def _quote_at(symbol: str, price: Decimal, at: datetime, half_spread_bps: Decimal) -> Quote:
    tick = Decimal("0.01")
    half = (price * half_spread_bps * BPS).quantize(tick)
    bid = max(price - half, tick)
    ask = price + half
    return Quote(
        symbol=symbol,
        ts=at,
        bid=bid,
        ask=ask,
        last=price,
        source="backtest",
        delayed=False,
    )


async def _drain_queue(broker: FakeBroker) -> AsyncIterator[Any]:
    while not broker._queue.empty():
        yield broker._queue.get_nowait()


async def _pump_events(broker: FakeBroker, core: Any) -> None:
    await core.handle_events(_drain_queue(broker))


def _visible_bars(
    all_bars: Mapping[str, Sequence[MarketBar]],
    decision_at: datetime,
    *,
    bar_feed: Callable[[datetime], Mapping[str, Sequence[MarketBar]]] | None = None,
) -> dict[str, tuple[MarketBar, ...]]:
    """截取或校验决策时刻可见的历史 K 线（bar.end <= decision_at）。"""
    if bar_feed is not None:
        supplied = bar_feed(decision_at)
        for symbol, seq in supplied.items():
            for bar in seq:
                if bar.end > decision_at:
                    raise LookaheadError(
                        f"前视泄漏：{symbol} 在 {decision_at} 收到尚未收盘的 K 线（收盘 {bar.end}）"
                    )
        return {s: tuple(seq) for s, seq in supplied.items()}
    return {
        symbol: tuple(b for b in seq if b.end <= decision_at) for symbol, seq in all_bars.items()
    }


def _bars_on_day(
    all_bars: Mapping[str, Sequence[MarketBar]], session: Session
) -> dict[str, MarketBar]:
    """取当日用于撮合与日末估值的 K 线（开收盘必须落在当日时段内）。"""
    today_bars: dict[str, MarketBar] = {}
    for symbol, seq in all_bars.items():
        matches = [b for b in seq if b.end.date() == session.day]
        if matches:
            today_bars[symbol] = matches[-1]
    return today_bars


def _stopped_out_dates(repo: LedgerRepository, calendar: TradingCalendar) -> dict[str, date]:
    stopped: dict[str, date] = {}
    for intent_id in repo.submitted_intents():
        it = repo.get_intent(intent_id)
        if it is not None and it.order_class is OrderClass.PROTECTIVE_STOP:
            for fill in repo.effective_executions_for(intent_id):
                stopped[fill.symbol] = calendar.session_day(fill.executed_at)
    return stopped


async def run_backtest(  # noqa: PLR0912, PLR0913, PLR0915 - 回测主循环按启动、逐日决策、撮合、收盘对账顺序组织
    strategy: Strategy,
    bars: Mapping[str, Sequence[MarketBar]],
    calendar: TradingCalendar,
    config: BacktestConfig,
    *,
    bar_feed: Callable[[datetime], Mapping[str, Sequence[MarketBar]]] | None = None,
    rebalance_days: Collection[date] = (),
) -> BacktestResult:
    """在独立临时账本与 SimBroker 上运行完整回测（走同一套风控、OMS 与日终对账）。"""
    sessions = list(calendar.sessions_between(config.start, config.end))
    if not sessions:
        raise ValueError("回测区间内没有交易时段")
    identity = identity_of(strategy)
    with tempfile.TemporaryDirectory(prefix="qts-bt-") as tmp_str:
        ledger_path = Path(tmp_str) / f"backtest-{config.experiment_id}.db"
        upgrade(ledger_path)
        repo = LedgerRepository(open_ledger(ledger_path))
        clock = _ManualClock(sessions[0].open)
        server = FakeBrokerServer(
            config.account,
            settled_cash=config.initial_cash,
            commission=config.commission,
            settlement=lambda dt: _next_session_day(calendar, dt),
            clock=clock,
        )
        broker = FakeBroker(server)
        quote_board = _QuoteBoard()
        sod_nav: dict[str, Decimal] = {config.account.account_id: config.initial_cash}
        peak_nav = [config.initial_cash]
        current_dd = [Decimal(0)]
        seen_rebalances: list[date] = []
        cooldown_source = LedgerStopCooldownSource(
            repo=repo,
            session_day=calendar.session_day,
            sessions_between=calendar.sessions_between,
            clock=clock,
            rebalance_days=lambda: seen_rebalances,
        )

        async def zero_skew() -> int | None:
            return 0

        inputs = CoreInputs(
            mode_config=ModeConfig(
                requested_mode=Mode.BACKTEST,
                account_id=config.account.account_id,
                expected_account_type=config.account.account_type,
                risk_regime=config.account.account_type,
                allow_delayed_data=False,
                config_version="backtest",
            ),
            instruments=config.instruments,
            components=BACKTEST_COMPONENTS,
            parameters=lambda: config.parameters,
            quotes=quote_board,
            account_synced=lambda: True,
            clock_skew_ms=zero_skew,
            start_of_day_nav=sod_nav.get,
            strategy_drawdown=lambda: {identity.strategy_id: current_dd[0]},
            stop_cooldown=cooldown_source,
            corporate_actions=_StaticCorporateActions(config.corporate_actions),
            broker_window=lambda _now: False,
            degraded_allowed={},
            session_day=calendar.session_day,
            clock=clock,
        )
        assembled = build_backtest_core(
            repo, broker, inputs, ledger_path=ledger_path, process_role="workerd"
        )
        await broker.connect()
        await _pump_events(broker, assembled.core)
        for comp in BACKTEST_COMPONENTS:
            assembled.run_state.report(comp.name, ok=True)
        startup = await assembled.core.start()
        if not (startup.recovered and startup.reconciled):
            raise BacktestError(f"回测启动对账未通过：{startup}")

        equity_curve: list[EquityPoint] = []
        outcomes: list[Outcome] = []
        all_sessions_so_far: list[date] = []
        rebalance_set = set(rebalance_days)

        for session in sessions:
            clock.set(session.open)
            server.settle(session.day)
            all_sessions_so_far.append(session.day)
            if session.day in rebalance_set:
                seen_rebalances.append(session.day)

            decision_at = session.open + config.decision_delay
            if decision_at >= session.close:
                raise LookaheadError(f"{session.day} 的决策时刻 {decision_at} 不早于收盘")
            clock.set(decision_at)
            for comp in BACKTEST_COMPONENTS:
                assembled.run_state.report(comp.name, ok=True)

            today_bars = _bars_on_day(bars, session)
            for sym, b in today_bars.items():
                quote_board.current[sym] = _quote_at(
                    sym, b.open, decision_at, config.half_spread_bps
                )

            held_map = {h.symbol: h.quantity for h in server.ledger.positions()}
            pos_val_open = sum(
                (
                    qty * (quote_board.current[sym].last or Decimal(0))
                    for sym, qty in held_map.items()
                ),
                Decimal(0),
            )
            nav_open = server.ledger.settled + server.ledger.unsettled + pos_val_open
            sod_nav[config.account.account_id] = nav_open
            peak_nav[0] = max(peak_nav[0], nav_open)
            current_dd[0] = (
                (peak_nav[0] - nav_open) / peak_nav[0] if peak_nav[0] > 0 else Decimal(0)
            )

            visible = _visible_bars(bars, decision_at, bar_feed=bar_feed)
            ctx = StrategyContext(
                decision_at=decision_at,
                session_day=session.day,
                account_id=config.account.account_id,
                bars=visible,
                positions=held_map,
                quotes=dict(quote_board.current),
                nav=nav_open,
                settled_cash=server.ledger.settled,
                sessions=tuple(all_sessions_so_far),
                stopped_out=_stopped_out_dates(repo, calendar),
                mode=Mode.BACKTEST.value,
            )
            try:
                intents = evaluate(strategy, ctx)
            except StrategyError as exc:
                if "前视" in str(exc) or "尚未收盘" in str(exc):
                    raise LookaheadError(str(exc)) from exc
                raise

            for intent in intents:
                outcome = await assembled.core.submit_intent(intent)
                outcomes.append(outcome)
                await _pump_events(broker, assembled.core)

            # K 线撮合：窗口 [decision_at + 1s, session.close]，成交时间 >= 下单时间 > 信号时间
            match_start = decision_at + timedelta(seconds=1)
            clock.set(match_start)
            for _sym, b in sorted(today_bars.items()):
                match_window_bar = MarketBar(
                    **{**b.model_dump(), "start": match_start, "end": session.close}
                )
                fills = server.match_bar(match_window_bar, config.participation)
                for f in fills:
                    if f.executed_at < decision_at:
                        raise LookaheadError("成交时间早于决策下单时间")
                await _pump_events(broker, assembled.core)

            clock.set(session.close)
            server.expire_day_orders()
            await _pump_events(broker, assembled.core)
            for sym, b in today_bars.items():
                quote_board.current[sym] = _quote_at(
                    sym, b.close, session.close, config.half_spread_bps
                )
            for comp in BACKTEST_COMPONENTS:
                assembled.run_state.report(comp.name, ok=True)
            if not await assembled.core.reconcile(ReconKind.END_OF_DAY):
                raise BacktestError(f"{session.day} 日终对账未通过")

            eod_positions = {h.symbol: h.quantity for h in server.ledger.positions()}
            eod_pos_value = sum(
                (
                    qty * (quote_board.current[sym].last or Decimal(0))
                    for sym, qty in eod_positions.items()
                ),
                Decimal(0),
            )
            eod_nav = server.ledger.settled + server.ledger.unsettled + eod_pos_value
            peak_nav[0] = max(peak_nav[0], eod_nav)
            all_execs = repo.effective_executions_between(
                sessions[0].open, session.close + timedelta(seconds=1), MAX_VIEW_ROWS
            )
            cum_comm = sum((e.commission for e in all_execs), Decimal(0))
            equity_curve.append(
                EquityPoint(
                    day=session.day,
                    at=session.close,
                    settled_cash=server.ledger.settled,
                    unsettled_cash=server.ledger.unsettled,
                    positions_value=eod_pos_value,
                    nav=eod_nav,
                    positions=eod_positions,
                    cumulative_commission=cum_comm,
                )
            )

        executions = tuple(
            repo.effective_executions_between(
                sessions[0].open, sessions[-1].close + timedelta(seconds=1), MAX_VIEW_ROWS
            )
        )
        await broker.disconnect()
        repo._engine.dispose()

    manifest = {
        "experiment_id": config.experiment_id,
        "strategy_id": identity.strategy_id,
        "strategy_version": identity.version,
        "universe": sorted(identity.universe),
        "start": config.start.isoformat(),
        "end": config.end.isoformat(),
        "initial_cash": str(config.initial_cash),
        "data_version": config.data_version,
        "code_commit": config.code_commit,
        "params_hash": config.params_hash,
        "risk_params_version": config.parameters.version,
        "cost_model": config.cost_model_name,
    }
    return BacktestResult(
        experiment_id=config.experiment_id,
        manifest=manifest,
        equity_curve=tuple(equity_curve),
        executions=executions,
        outcomes=tuple(outcomes),
    )


# ---------- T-04d：第二套独立实现（月度双资产动量，不共享 OMS / 风控 / SimBroker） ----------


class DualMomentumStrategy:
    """月度双资产（如 SPY / IEF）二选一动量策略（用于 T-04d 与独立向量化实现复算对照）。"""

    def __init__(
        self,
        pair: tuple[str, str] = ("SPY", "IEF"),
        *,
        lookback: int = 20,
        allocation: Decimal = Decimal("0.04"),
    ) -> None:
        self._pair = pair
        self._lookback = lookback
        self._allocation = allocation

    @property
    def strategy_id(self) -> str:
        return "dual_momentum"

    @property
    def version(self) -> str:
        return "v1"

    @property
    def universe(self) -> frozenset[str]:
        return frozenset(self._pair)

    def decide(self, context: StrategyContext) -> Sequence[Proposal]:  # noqa: PLR0911
        sessions = context.sessions
        if not sessions:
            return ()
        idx = len(sessions) - 1
        day = sessions[-1]
        first_of_month = idx == 0 or (day.year, day.month) != (
            sessions[idx - 1].year,
            sessions[idx - 1].month,
        )
        second_of_month = idx >= 1 and (
            idx == 1
            or (sessions[idx - 1].year, sessions[idx - 1].month)
            != (sessions[idx - 2].year, sessions[idx - 2].month)
        )
        if not (first_of_month or second_of_month):
            return ()
        a, b = self._pair
        bars_a, bars_b = context.bars.get(a, ()), context.bars.get(b, ())
        # 在每月首个交易日及次日（先卖后买），用调仓日前一交易日为止的收盘价计算动量
        offset = 1 if first_of_month else 2
        if len(bars_a) < self._lookback + offset or len(bars_b) < self._lookback + offset:
            return ()
        ret_a = bars_a[-offset].close / bars_a[-offset - self._lookback].close - Decimal(1)
        ret_b = bars_b[-offset].close / bars_b[-offset - self._lookback].close - Decimal(1)
        winner = a if ret_a >= ret_b else b
        loser = b if winner == a else a
        held_loser = context.positions.get(loser, Decimal(0))
        held_winner = context.positions.get(winner, Decimal(0))
        if first_of_month and held_loser > 0:
            px = context.quotes[loser].last
            if px is None:
                return ()
            return (
                Proposal(
                    symbol=loser,
                    side=Side.SELL,
                    quantity=held_loser,
                    limit_price=px,
                    order_class=OrderClass.REDUCE,
                    reason="月度动量切换卖出旧持仓",
                ),
            )
        if held_winner == 0 and held_loser == 0 and context.settled_cash is not None:
            px = context.quotes[winner].last
            if px is None:
                return ()
            qty = (context.settled_cash * self._allocation / px).to_integral_value(
                rounding=ROUND_FLOOR
            )
            if qty > 0:
                return (
                    Proposal(
                        symbol=winner,
                        side=Side.BUY,
                        quantity=qty,
                        limit_price=px,
                        order_class=OrderClass.OPEN,
                        reason="月度动量买入胜出标的",
                    ),
                )
        return ()


@dataclass(frozen=True)
class ReferenceDay:
    day: date
    positions: Mapping[str, Decimal]
    nav: Decimal


def vectorized_dual_momentum_reference(  # noqa: PLR0913 - 对照实现的输入全部显式列出
    bars: Mapping[str, Sequence[MarketBar]],
    sessions: Sequence[Session],
    *,
    pair: tuple[str, str] = ("SPY", "IEF"),
    lookback: int = 20,
    initial_cash: Decimal = Decimal(100_000),
    allocation: Decimal = Decimal("0.04"),
    commission: CommissionModel | None = None,
) -> tuple[ReferenceDay, ...]:
    """T-04d 独立复算实现：每月首个交易日比较两只 ETF 的过去 lookback 日涨幅，切换至胜出者。

    规则（先卖后买，与 DualMomentumStrategy 完全对应）：
    - 每月首个交易日若持有落败标的：当日开盘卖出落败标的（款项 T+1 结算），
      次个交易日开盘买入胜出标的；
    - 每月首个交易日若空仓：当日开盘按 allocation × 已结算现金 向下取整股买入胜出标的。
    """
    comm_fn = commission or NoCommission()
    a, b = pair
    by_day: dict[str, dict[date, MarketBar]] = {
        sym: {bar.end.date(): bar for bar in bars[sym]} for sym in pair
    }
    cash = initial_cash
    unsettled: list[tuple[date, Decimal]] = []
    held_sym: str | None = None
    held_qty = Decimal(0)
    pending_target: str | None = None
    out: list[ReferenceDay] = []
    history_days: list[date] = []

    for idx, session in enumerate(sessions):
        day = session.day
        still_pending: list[tuple[date, Decimal]] = []
        for settle_day, amount in unsettled:
            if settle_day <= day:
                cash += amount
            else:
                still_pending.append((settle_day, amount))
        unsettled = still_pending

        first_of_month = idx == 0 or (day.year, day.month) != (
            sessions[idx - 1].day.year,
            sessions[idx - 1].day.month,
        )
        if first_of_month and len(history_days) > lookback:
            d_now = history_days[-1]
            d_prev = history_days[-1 - lookback]
            ret_a = by_day[a][d_now].close / by_day[a][d_prev].close - Decimal(1)
            ret_b = by_day[b][d_now].close / by_day[b][d_prev].close - Decimal(1)
            winner = a if ret_a >= ret_b else b
            if held_sym != winner:
                if held_sym is not None and held_qty > 0:
                    sell_px = by_day[held_sym][day].open
                    comm = comm_fn(held_qty, sell_px)
                    cash -= comm
                    next_day = sessions[idx + 1].day if idx + 1 < len(sessions) else day
                    unsettled.append((next_day, held_qty * sell_px))
                    held_sym, held_qty = None, Decimal(0)
                    pending_target = winner
                else:
                    pending_target = winner

        if pending_target is not None and held_qty == 0 and not unsettled:
            buy_px = by_day[pending_target][day].open
            budget = cash * allocation
            qty = (budget / buy_px).to_integral_value(rounding=ROUND_FLOOR)
            if qty > 0:
                comm = comm_fn(qty, buy_px)
                cash -= qty * buy_px + comm
                held_sym, held_qty = pending_target, qty
            pending_target = None

        history_days.append(day)
        pos_map = {held_sym: held_qty} if held_sym is not None and held_qty > 0 else {}
        pos_val = (
            held_qty * by_day[held_sym][day].close
            if held_sym is not None and held_qty > 0
            else Decimal(0)
        )
        unsettled_sum = sum((amt for _, amt in unsettled), Decimal(0))
        out.append(ReferenceDay(day=day, positions=pos_map, nav=cash + unsettled_sum + pos_val))
    return tuple(out)


__all__ = (
    "BACKTEST_COMPONENTS",
    "BacktestConfig",
    "BacktestError",
    "BacktestResult",
    "DualMomentumStrategy",
    "EquityPoint",
    "LookaheadError",
    "ReferenceDay",
    "run_backtest",
    "vectorized_dual_momentum_reference",
)
