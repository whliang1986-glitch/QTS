"""ModeManager：运行模式的唯一事实来源与实盘切换门禁（C-04，V4.1 6.2）。

- 模式只来自账本里持久化的记录，不靠界面按钮或环境变量。
- 每次模式变化与相关审计在同一个事务里写入（不会出现“审计已写、HALT 未写”）。
- 启动时校验配置、账户 ID、账户类型与风控口径；任何歧义都持久化 HALT 并拒绝。
- 本进程内一旦启动被拒或写入失败，即闩锁为拒绝状态，不依赖账本
  （tradingd 遇到 start 抛出的任何异常都应退出）。
- HALT 持久化，重启后不自动恢复；恢复需要本进程启动校验通过、全部检查通过、由人工操作者发起。
- 实盘模式（ASSISTED_LIVE、LIMITED_AUTO_LIVE）在 G7 之前无条件拒绝：代码里没有解锁开关，
  数据库触发器（迁移 0002）也拒绝写入；G7 需要新增代码路径与经 Dylan 批准的迁移。
- ADR-06：开发期“允许延迟行情”开关只在 PAPER 下可用（配置校验），打开时任何实盘请求都被拒。
- ADR-15：真实账户类型以券商为准；受保护配置登记期望的账户类型，不一致即进入 SAFE HALT；
  放宽口径（MARGIN）必须与真实类型一致；同一账户类型变化、或换了账户，
  都进入 SAFE HALT 等待人工确认。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import ClassVar, Self
from uuid import uuid4

from pydantic import StrictBool, model_validator

from server.domain.account import Account
from server.domain.enums import LIVE_MODES, AccountType, Mode
from server.domain.modes import AuditEvent, HaltState
from server.domain.values import Identifier, ValueModel
from server.storage.ledger import LedgerRepository, ModeChange
from server.storage.logs import mask_account

SYSTEM_ACTOR = "system"
# 这些名字代表程序而不是人，不能用来恢复交易（比较时不区分大小写）
NON_HUMAN_ACTORS = frozenset(
    {"system", "tradingd", "apid", "workerd", "guardd", "unknown-operator"}
)
# 首期唯一允许新增风险的模式
TRADING_MODES = frozenset({Mode.PAPER})

Detail = tuple[tuple[str, str], ...]


class ModeError(Exception):
    """模式相关的拒绝。"""


class StartupRefusedError(ModeError):
    """启动校验不通过（已尽力持久化 HALT；本进程闩锁为拒绝）。"""


class LiveModeLockedError(ModeError):
    """实盘模式被门禁锁定。"""


class TradingHaltedError(ModeError):
    """当前不允许新增风险（HALT、未初始化、本进程已闩锁或恢复条件不满足）。"""


class ModeConfig(ValueModel):
    """启动配置（由 C-05 从受保护配置加载并版本化）。"""

    requested_mode: Mode
    account_id: Identifier
    # 受保护配置登记的真实账户类型；账户升级时由 Dylan 修改
    expected_account_type: AccountType
    # 风控口径：CASH 只用已结算现金、杠杆 1.0；MARGIN 需与真实账户一致
    risk_regime: AccountType
    allow_delayed_data: StrictBool = False
    config_version: Identifier

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.allow_delayed_data and self.requested_mode is not Mode.PAPER:
            raise ValueError("允许延迟行情的开关只能在 PAPER 模式下打开（ADR-06）")
        margin = AccountType.MARGIN
        if self.risk_regime is margin and self.expected_account_type is not margin:
            raise ValueError("保证金口径要求登记的账户类型也是保证金")
        return self


class ResumeChecks(ValueModel):
    """从 HALT 恢复前必须全部通过的检查（V4.1 7.3）。只接受真正的布尔值。"""

    evolvable_fields: ClassVar[frozenset[str] | None] = None

    data_ok: StrictBool
    account_ok: StrictBool
    orders_ok: StrictBool
    risk_ok: StrictBool
    database_ok: StrictBool
    reconciled: StrictBool


def _normalize_actor(actor: str) -> str:
    cleaned = re.sub(r"\s+", "_", actor.strip())[:128]
    return cleaned or "unknown-operator"


class ModeManager:
    def __init__(
        self,
        repo: LedgerRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._new_id = new_id
        self._refused: str | None = None  # 进程内闩锁
        self._verified: tuple[ModeConfig, Account] | None = None

    # ---------- 查询 ----------

    def current(self) -> ModeChange | None:
        return self._repo.latest_mode()

    def require_trading_allowed(self) -> None:
        """风控放行前调用：闩锁、无状态、HALT、非交易模式一律拒绝（失败即关闭）。"""
        if self._refused is not None:
            raise TradingHaltedError(f"本进程启动校验未通过：{self._refused}")
        if self._verified is None:  # 只信任在本实例里完成过启动校验的状态
            raise TradingHaltedError("本实例尚未完成启动校验，拒绝新增风险")
        state = self.current()
        if state is None:  # pragma: no cover - 启动校验通过后一定已有状态
            raise TradingHaltedError("尚未完成启动校验，拒绝新增风险")
        if state.mode is Mode.HALT or state.halt.halted:
            raise TradingHaltedError(f"处于 HALT：{state.halt.reason}")
        if state.mode not in TRADING_MODES:
            raise TradingHaltedError(f"{state.mode.value} 模式不允许新增风险")

    # ---------- 启动 ----------

    def start(self, config: ModeConfig, account: Account) -> ModeChange:
        try:
            return self._start(config, account)
        except ModeError as exc:
            self._refused = str(exc)
            raise
        except Exception as exc:
            self._refused = f"启动过程出错：{exc}"
            raise StartupRefusedError(self._refused) from exc

    def _start(self, config: ModeConfig, account: Account) -> ModeChange:
        observed = self._event(
            "account_observed",
            SYSTEM_ACTOR,
            (
                ("account_id", account.account_id),
                ("account_type", account.account_type.value),
                ("config_version", config.config_version),
                ("requested_mode", config.requested_mode.value),
                ("delayed_data", str(config.allow_delayed_data)),
            ),
        )

        refusal = self._refusal_reason(config, account)
        if refusal is not None:
            exc_type, reason = refusal
            self._commit_halt(f"启动被拒：{reason}", SYSTEM_ACTOR, [observed])
            raise exc_type(reason)

        # 本次观察尚未写入，比较的是以前的记录
        safe_halt = self._safe_halt_reason(config, account)
        self._verified = (config, account)
        if safe_halt is not None:
            return self._commit_halt(safe_halt, SYSTEM_ACTOR, [observed])

        latest = self.current()
        if latest is not None and (latest.mode is Mode.HALT or latest.halt.halted):
            kept = self._event(
                "startup_kept_halt", SYSTEM_ACTOR, (("reason", latest.halt.reason or ""),)
            )
            self._repo.commit_mode_and_audits([observed, kept])
            return latest

        started = (("config_version", config.config_version),)
        return self._commit(
            config.requested_mode,
            HaltState(halted=False),
            SYSTEM_ACTOR,
            [observed],
            ("mode_started", started),
        )

    @staticmethod
    def _refusal_reason(config: ModeConfig, account: Account) -> tuple[type[ModeError], str] | None:
        if config.requested_mode in LIVE_MODES:
            return LiveModeLockedError, f"{config.requested_mode.value} 在 G7 之前锁定"
        if config.requested_mode is Mode.HALT:
            return StartupRefusedError, "启动模式不能是 HALT；HALT 只能由急停或校验失败产生"
        if account.account_id != config.account_id:
            return StartupRefusedError, (
                f"账户 ID 不一致：配置 {mask_account(config.account_id)}，"
                f"券商 {mask_account(account.account_id)}"
            )
        margin = AccountType.MARGIN
        if config.risk_regime is margin and account.account_type is not margin:
            return StartupRefusedError, (
                "风控口径为保证金，但真实账户不是保证金账户；放宽口径必须与真实账户类型一致"
            )
        return None

    def _safe_halt_reason(self, config: ModeConfig, account: Account) -> str | None:
        if account.account_type is not config.expected_account_type:
            return (
                f"账户类型与登记不一致：登记 {config.expected_account_type.value}，"
                f"券商 {account.account_type.value}；需 Dylan 修改受保护配置并人工恢复"
            )
        history = self._repo.audits("account_observed")
        if history:
            last_account = dict(history[0].detail).get("account_id")
            if last_account is not None and last_account != account.account_id:
                return (
                    f"账户变更：上次 {mask_account(last_account)}，"
                    f"本次 {mask_account(account.account_id)}；需人工确认"
                )
        for event in history:
            detail = dict(event.detail)
            if detail.get("account_id") == account.account_id:
                previous = detail.get("account_type")
                if previous != account.account_type.value:
                    return f"账户类型变化：{previous} → {account.account_type.value}；需人工确认"
                break
        return None

    def observe_account(self, account: Account) -> ModeChange | None:
        """重连时调用（B-03）：账户 ID 或类型与本进程校验时不同，立即进入 SAFE HALT。"""
        if self._verified is None:
            raise TradingHaltedError("尚未完成启动校验")
        _, verified = self._verified
        same_id = account.account_id == verified.account_id
        if same_id and account.account_type is verified.account_type:
            return self.current()
        observed = self._event(
            "account_observed",
            SYSTEM_ACTOR,
            (("account_id", account.account_id), ("account_type", account.account_type.value)),
        )
        reason = "重连后账户 ID 或类型与启动时不同；需重启并重新完成启动校验"
        # 闩锁：同一进程里不能恢复到换掉的账户上，必须重启走完整的 start 校验
        self._refused = reason
        return self._commit_halt(reason, SYSTEM_ACTOR, [observed])

    # ---------- 急停与恢复 ----------

    def halt(self, reason: str, *, actor: str) -> ModeChange:
        """急停：对输入容错，保证一定尝试落盘；写入失败时本进程闩锁为拒绝并抛出。"""
        text = reason.strip() or "未说明原因"
        who = _normalize_actor(actor)
        try:
            return self._commit_halt(text, who, [])
        except Exception:
            self._refused = f"HALT 写入失败：{text}"
            raise

    def resume(self, target: Mode, *, actor: str, checks: ResumeChecks) -> ModeChange:
        who = _normalize_actor(actor)
        try:
            self._check_resume(target, who, checks)
        except ModeError as exc:
            detail = (("target", Mode(target).value), ("reason", str(exc)))
            self._repo.commit_mode_and_audits([self._event("resume_refused", who, detail)])
            raise
        return self._commit(
            target, HaltState(halted=False), who, [], ("resumed", (("target", target.value),))
        )

    def _check_resume(self, target: Mode, who: str, checks: ResumeChecks) -> None:
        if Mode(target) in LIVE_MODES:
            raise LiveModeLockedError("G7 之前不能恢复到实盘模式")
        if target is Mode.HALT:
            raise TradingHaltedError("恢复目标不能是 HALT")
        if self._refused is not None:
            raise TradingHaltedError(f"本进程启动校验未通过：{self._refused}")
        if self._verified is None:
            raise TradingHaltedError("本进程尚未通过启动校验，不能恢复")
        config, account = self._verified
        if account.account_type is not config.expected_account_type:
            raise TradingHaltedError("账户类型与受保护配置登记不一致：请先修改配置并重启")
        if target is not config.requested_mode:
            raise TradingHaltedError(f"恢复目标必须是配置请求的模式 {config.requested_mode.value}")
        state = self.current()
        if state is None or not (state.mode is Mode.HALT or state.halt.halted):
            raise TradingHaltedError("当前不在 HALT，无需恢复")
        failed = [name for name, ok in checks.model_dump().items() if not ok]
        if failed:
            raise TradingHaltedError(f"恢复条件未满足：{', '.join(failed)}")
        if who.lower() in NON_HUMAN_ACTORS:
            raise TradingHaltedError("HALT 只能由人工操作者恢复")

    def request_live_switch(self, target: Mode, *, actor: str, confirmation: str) -> ModeChange:
        """切换到实盘：G7 之前一律拒绝。G7 任务需另行实现账户、限额与二次确认令牌。"""
        del confirmation
        detail = (("target", Mode(target).value),)
        self._repo.commit_mode_and_audits(
            [self._event("live_switch_refused", _normalize_actor(actor), detail)]
        )
        raise LiveModeLockedError("G7 之前禁止切换到实盘模式")

    # ---------- 内部 ----------

    def _event(self, action: str, actor: str, detail: Detail) -> AuditEvent:
        return AuditEvent(
            event_id=self._new_id(),
            occurred_at=self._clock(),
            actor=actor,
            action=action,
            correlation_id=self._new_id(),
            detail=detail,
        )

    def _commit_halt(self, reason: str, actor: str, extra: list[AuditEvent]) -> ModeChange:
        halt = HaltState(halted=True, reason=reason, since=self._clock(), set_by=actor)
        return self._commit(Mode.HALT, halt, actor, extra, ("halt", (("reason", reason),)))

    def _commit(
        self,
        mode: Mode,
        halt: HaltState,
        actor: str,
        extra: list[AuditEvent],
        audit: tuple[str, Detail],
    ) -> ModeChange:
        """模式变化与审计同一事务写入。"""
        action, detail = audit
        event = self._event(action, actor, (("mode", mode.value), *detail))
        self._repo.commit_mode_and_audits(
            [*extra, event], (self._new_id(), mode, halt, event.occurred_at, actor)
        )
        state = self.current()
        if state is None:  # pragma: no cover - 刚写入，不可能为空
            raise TradingHaltedError("模式写入后读取失败")
        return state
