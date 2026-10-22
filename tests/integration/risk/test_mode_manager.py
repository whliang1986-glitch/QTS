"""C-04：ModeManager 与实盘切换门禁——真实 SQLite 账本，不用 mock。"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from server.domain.account import Account
from server.domain.enums import AccountType, Mode
from server.domain.modes import AuditEvent, HaltState
from server.risk.mode_manager import (
    LiveModeLockedError,
    ModeConfig,
    ModeManager,
    ResumeChecks,
    StartupRefusedError,
    TradingHaltedError,
)
from server.storage.errors import StorageError
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import mask_account
from server.storage.migrate import upgrade

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ALL_OK = ResumeChecks(
    data_ok=True, account_ok=True, orders_ok=True, risk_ok=True, database_ok=True, reconciled=True
)
CASH, MARGIN = AccountType.CASH, AccountType.MARGIN


def config(**overrides: Any) -> ModeConfig:
    data: dict[str, Any] = {
        "requested_mode": Mode.PAPER,
        "account_id": "DU1234567",
        "expected_account_type": CASH,
        "risk_regime": CASH,
        "allow_delayed_data": False,
        "config_version": "risk-v1",
    }
    data.update(overrides)
    return ModeConfig.model_validate(data)


def account(account_type: AccountType = CASH, account_id: str = "DU1234567") -> Account:
    return Account(account_id=account_id, account_type=account_type, base_currency="USD")


class FlakyRepo(LedgerRepository):
    """真实账本，只是让第 N 次“模式与审计”写入失败，用来注入存储故障。"""

    fail_on: int | None = None
    calls = 0

    def commit_mode_and_audits(
        self,
        audits: Sequence[AuditEvent],
        change: tuple[str, Mode, HaltState, datetime, str] | None = None,
    ) -> None:
        type(self).calls += 1
        if type(self).fail_on == type(self).calls:
            raise StorageError("注入的写入失败")
        super().commit_mode_and_audits(audits, change)


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    upgrade(path)
    FlakyRepo.fail_on, FlakyRepo.calls = None, 0
    return path


@pytest.fixture
def repo(ledger_path: Path) -> FlakyRepo:
    return FlakyRepo(open_ledger(ledger_path))


@pytest.fixture
def make_manager(repo: FlakyRepo) -> Callable[[], ModeManager]:
    ticks = itertools.count()
    ids = itertools.count()

    def clock() -> datetime:
        return T0 + timedelta(seconds=next(ticks))

    def new_id() -> str:
        return f"id-{next(ids)}"

    return lambda: ModeManager(repo, clock=clock, new_id=new_id)


Make = Callable[[], ModeManager]


def resumed(make: Make, cfg: ModeConfig, acct: Account) -> ModeManager:
    mm = make()
    mm.start(cfg, acct)
    mm.resume(cfg.requested_mode, actor="dylan", checks=ALL_OK)
    return mm


# ---------- 正常启动 ----------


def test_first_start_enters_requested_paper_mode(make_manager: Make) -> None:
    mm = make_manager()
    state = mm.start(config(), account())
    assert state.mode is Mode.PAPER and not state.halt.halted
    mm.require_trading_allowed()


def test_cash_regime_on_margin_account_is_allowed(make_manager: Make) -> None:
    # 从严口径：保证金账户也可以按现金规则运行
    cfg = config(expected_account_type=MARGIN, risk_regime=CASH)
    assert make_manager().start(cfg, account(MARGIN)).mode is Mode.PAPER


def test_margin_regime_on_margin_account_is_allowed(make_manager: Make) -> None:
    cfg = config(expected_account_type=MARGIN, risk_regime=MARGIN)
    assert make_manager().start(cfg, account(MARGIN)).mode is Mode.PAPER


# ---------- 配置本身的校验 ----------


@pytest.mark.parametrize(
    "bad",
    [
        {"allow_delayed_data": True, "requested_mode": Mode.RESEARCH},
        {"allow_delayed_data": True, "requested_mode": Mode.ASSISTED_LIVE},
        {"allow_delayed_data": "yes"},
        {"risk_regime": MARGIN, "expected_account_type": CASH},
    ],
)
def test_inconsistent_config_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        config(**bad)


def test_resume_checks_require_real_booleans() -> None:
    with pytest.raises(ValidationError):
        ResumeChecks.model_validate({**ALL_OK.model_dump(), "reconciled": "yes"})


# ---------- 拒绝启动：持久化 HALT + 进程闩锁 ----------


@pytest.mark.parametrize(
    ("cfg", "acct", "reason"),
    [
        ({}, {"account_id": "DU7654321"}, "账户 ID"),
        ({"risk_regime": MARGIN, "expected_account_type": MARGIN}, {}, "口径"),
        ({"requested_mode": Mode.HALT}, {}, "HALT"),
    ],
)
def test_ambiguous_startup_is_refused_and_halted(
    make_manager: Make, cfg: dict[str, Any], acct: dict[str, Any], reason: str
) -> None:
    mm = make_manager()
    with pytest.raises(StartupRefusedError, match=reason):
        mm.start(config(**cfg), account(**acct))
    current = make_manager().current()
    assert current is not None and current.mode is Mode.HALT and current.halt.halted
    with pytest.raises(TradingHaltedError):
        mm.require_trading_allowed()
    with pytest.raises(TradingHaltedError):  # 被拒的进程不能自行恢复
        mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)


def test_account_numbers_are_masked_in_reasons(make_manager: Make) -> None:
    with pytest.raises(StartupRefusedError) as info:
        make_manager().start(config(), account(account_id="DU7654321"))
    assert "DU7654321" not in str(info.value) and "DU****321" in str(info.value)
    current = make_manager().current()
    assert current is not None and "DU1234567" not in (current.halt.reason or "")
    assert mask_account("DU1234567") == "DU****567"


def test_storage_failure_during_start_latches_refusal(repo: FlakyRepo, make_manager: Make) -> None:
    resumed_mm = make_manager()
    resumed_mm.start(config(), account())  # 账本里已是 PAPER
    FlakyRepo.fail_on = FlakyRepo.calls + 1
    mm = make_manager()
    with pytest.raises(StartupRefusedError, match="注入"):
        mm.start(config(), account())
    with pytest.raises(TradingHaltedError, match="启动校验未通过"):
        mm.require_trading_allowed()  # 账本仍显示 PAPER，但本进程闩锁拒绝
    del repo


def test_failed_type_change_halt_is_not_silently_skipped(
    repo: FlakyRepo, make_manager: Make
) -> None:
    # 评审 C-04 B1：类型变化的 HALT 写入失败后，重启不能静默进入 PAPER
    make_manager().start(config(expected_account_type=MARGIN, risk_regime=CASH), account(MARGIN))
    FlakyRepo.fail_on = FlakyRepo.calls + 1
    upgraded_cfg = config(expected_account_type=CASH)  # 登记仍是现金
    with pytest.raises(StartupRefusedError):
        make_manager().start(upgraded_cfg, account(CASH))
    state = make_manager().start(upgraded_cfg, account(CASH))
    assert state.mode is Mode.HALT and "账户类型变化" in (state.halt.reason or "")
    del repo


# ---------- 实盘门禁（G7 之前锁定） ----------


@pytest.mark.parametrize("mode", [Mode.ASSISTED_LIVE, Mode.LIMITED_AUTO_LIVE])
def test_live_modes_are_locked_on_every_path(make_manager: Make, mode: Mode) -> None:
    with pytest.raises(LiveModeLockedError):
        make_manager().start(config(requested_mode=mode), account())
    mm = make_manager()
    mm.start(config(), account())
    mm.halt("测试", actor="dylan")
    with pytest.raises(LiveModeLockedError):
        mm.resume(mode, actor="dylan", checks=ALL_OK)
    with pytest.raises(LiveModeLockedError):
        mm.request_live_switch(mode, actor="dylan", confirmation="yes")


def test_delayed_data_allowed_in_paper_and_audited(repo: FlakyRepo, make_manager: Make) -> None:
    assert make_manager().start(config(allow_delayed_data=True), account()).mode is Mode.PAPER
    observed = repo.latest_audit("account_observed")
    assert observed is not None and dict(observed.detail)["delayed_data"] == "True"


def test_non_paper_modes_do_not_allow_trading(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(requested_mode=Mode.RESEARCH), account())
    with pytest.raises(TradingHaltedError, match="RESEARCH"):
        mm.require_trading_allowed()


# ---------- HALT 持久化与恢复 ----------


def test_halt_survives_restart(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    mm.halt("急停：停止新增订单", actor="dylan")
    restarted = make_manager()
    state = restarted.start(config(), account())  # 新进程，配置照常请求 PAPER
    assert state.mode is Mode.HALT and state.halt.halted
    with pytest.raises(TradingHaltedError):
        restarted.require_trading_allowed()


def test_halt_tolerates_bad_input(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    state = mm.halt("   ", actor="Dylan Liang")
    assert state.halt.halted and state.halt.reason == "未说明原因"
    assert state.halt.set_by == "Dylan_Liang"
    assert mm.halt("x", actor="").halt.set_by == "unknown-operator"


def test_resume_requires_every_check(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    mm.halt("测试", actor="dylan")
    for field in ResumeChecks.model_fields:
        with pytest.raises(TradingHaltedError, match=field):
            mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK.evolve(**{field: False}))
    for robot in ("system", "System", "guardd", ""):
        with pytest.raises(TradingHaltedError, match="人工"):
            mm.resume(Mode.PAPER, actor=robot, checks=ALL_OK)
    with pytest.raises(TradingHaltedError, match="配置请求的模式"):
        mm.resume(Mode.RESEARCH, actor="dylan", checks=ALL_OK)
    state = mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)
    assert state.mode is Mode.PAPER and not state.halt.halted
    mm.require_trading_allowed()


def test_resume_requires_verified_start_in_this_process(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    mm.halt("测试", actor="dylan")
    fresh = make_manager()  # 没有调用 start 的新实例
    with pytest.raises(TradingHaltedError, match="启动校验"):
        fresh.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)


def test_resume_only_from_halt(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    with pytest.raises(TradingHaltedError, match="不在 HALT"):
        mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)


def test_no_state_means_no_trading(make_manager: Make) -> None:
    with pytest.raises(TradingHaltedError):
        make_manager().require_trading_allowed()


def test_instance_without_start_never_allows_trading(make_manager: Make) -> None:
    # 评审 C-04 R2：账本里是 PAPER，但这个实例没有做过启动校验
    make_manager().start(config(), account())
    with pytest.raises(TradingHaltedError, match="启动校验"):
        make_manager().require_trading_allowed()


# ---------- 账户类型与账户变更（ADR-15） ----------


def test_type_mismatch_with_registered_config_enters_safe_halt(make_manager: Make) -> None:
    make_manager().start(config(), account(CASH))
    # 账户在 IBKR 升级为保证金，但受保护配置仍登记现金
    mm = make_manager()
    state = mm.start(config(), account(MARGIN))
    assert state.mode is Mode.HALT and "登记" in (state.halt.reason or "")
    with pytest.raises(TradingHaltedError, match="修改配置"):
        mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)


def test_type_change_flow_after_config_update(make_manager: Make) -> None:
    make_manager().start(config(), account(CASH))
    upgraded = config(expected_account_type=MARGIN)  # Dylan 已修改受保护配置（口径仍是现金）
    state = make_manager().start(upgraded, account(MARGIN))
    assert state.mode is Mode.HALT and "账户类型变化" in (state.halt.reason or "")
    resumed(make_manager, upgraded, account(MARGIN))
    # 人工恢复后再重启：不会因为同一次变化再次 HALT
    assert make_manager().start(upgraded, account(MARGIN)).mode is Mode.PAPER


def test_switching_accounts_requires_confirmation(make_manager: Make) -> None:
    make_manager().start(config(), account(CASH))
    other = config(account_id="DU7654321")
    state = make_manager().start(other, account(CASH, "DU7654321"))
    assert state.mode is Mode.HALT and "账户变更" in (state.halt.reason or "")


def test_type_change_detected_per_account_after_switching_back(make_manager: Make) -> None:
    # 评审 C-04 I1：A(CASH) → B → A(MARGIN)，A 的类型变化不能漏判
    a_cash, b = config(), config(account_id="DU7654321")
    make_manager().start(a_cash, account(CASH))
    resumed(make_manager, b, account(CASH, "DU7654321"))
    a_margin = config(expected_account_type=MARGIN)
    state = make_manager().start(a_margin, account(MARGIN))
    assert state.mode is Mode.HALT
    reasons = state.halt.reason or ""
    assert "账户变更" in reasons or "账户类型变化" in reasons


def test_reconnect_with_different_account_enters_safe_halt(make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    assert mm.observe_account(account()) is not None
    state = mm.observe_account(account(MARGIN))
    assert state is not None and state.mode is Mode.HALT
    # 评审 C-04 R1：同一进程不能恢复到换掉的账户上
    with pytest.raises(TradingHaltedError, match="启动校验"):
        mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)
    with pytest.raises(TradingHaltedError):
        mm.require_trading_allowed()


# ---------- 审计 ----------


def test_every_path_is_audited(repo: FlakyRepo, make_manager: Make) -> None:
    mm = make_manager()
    mm.start(config(), account())
    mm.halt("测试", actor="dylan")
    with pytest.raises(TradingHaltedError):
        mm.resume(Mode.PAPER, actor="system", checks=ALL_OK)
    mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)
    with pytest.raises(LiveModeLockedError):
        mm.request_live_switch(Mode.ASSISTED_LIVE, actor="dylan", confirmation="x")
    make_manager().start(config(), account())
    mm.halt("再次", actor="dylan")
    make_manager().start(config(), account())
    for action in (
        "account_observed",
        "mode_started",
        "halt",
        "resume_refused",
        "resumed",
        "live_switch_refused",
        "startup_kept_halt",
    ):
        assert repo.latest_audit(action) is not None, action


def test_upgrade_flow_when_started_before_config_update(make_manager: Make) -> None:
    # 评审 C-04 S-a：先在升级后的账户上启动（配置仍登记现金），再改配置重启
    make_manager().start(config(), account(CASH))
    first = make_manager().start(config(), account(MARGIN))
    assert first.mode is Mode.HALT and "登记" in (first.halt.reason or "")
    upgraded = config(expected_account_type=MARGIN)
    mm = make_manager()
    kept = mm.start(upgraded, account(MARGIN))
    assert kept.mode is Mode.HALT  # 重启不会自动解除
    state = mm.resume(Mode.PAPER, actor="dylan", checks=ALL_OK)  # 配置已与真实类型一致，可人工恢复
    assert state.mode is Mode.PAPER
    assert make_manager().start(upgraded, account(MARGIN)).mode is Mode.PAPER
