"""急停状态与审计事件（C-01）。模式切换逻辑在 C-04 的 ModeManager。"""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from server.domain.values import Identifier, UtcDatetime, ValueModel


class HaltState(ValueModel):
    """HALT 持久化，重启不自动恢复（V4.1 7.3）。"""

    halted: bool
    reason: str | None = None
    since: UtcDatetime | None = None
    set_by: Identifier | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.halted and not (self.reason and self.since and self.set_by):
            raise ValueError("进入 HALT 必须记录原因、时间与操作者")
        return self


class AuditEvent(ValueModel):
    event_id: Identifier
    occurred_at: UtcDatetime
    actor: Identifier
    action: Identifier
    correlation_id: Identifier
    detail: tuple[tuple[str, str], ...] = ()


class StrategyVersion(ValueModel):
    strategy_id: Identifier
    version: Identifier
    params_hash: Identifier
