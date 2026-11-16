"""回测引擎，复用 oms 与 risk（T-04）。"""

from server.backtest.engine import (
    BACKTEST_COMPONENTS,
    BacktestConfig,
    BacktestError,
    BacktestResult,
    DualMomentumStrategy,
    EquityPoint,
    LookaheadError,
    ReferenceDay,
    run_backtest,
    vectorized_dual_momentum_reference,
)

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
