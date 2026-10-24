"""R-02a / ADR-16：风控参数加载、运行时调严与二次确认调松、重启重放（真实账本）。

所有数值都是测试夹具，写在临时目录里，不是 Dylan 批准的参数，也不触碰受保护配置目录。
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.domain.account import Position
from server.domain.enums import OrderClass, OrderType, Side, TimeInForce
from server.domain.modes import AuditEvent
from server.domain.orders import PROTECTIVE_STOP_SOURCE, OrderIntent
from server.risk.engine import RiskContext, Subject
from server.risk.parameters import (
    SPECS,
    Kind,
    ParameterConfig,
    ParameterError,
    ParameterStore,
    parse_parameters,
)
from server.risk.rulesets import build_rule_set, rule_source
from server.storage.config import ConfigError, load_toml
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.migrate import upgrade
from tests.support.oms import ACCOUNT, SPY

T0 = datetime(2026, 10, 24, 14, 0, tzinfo=UTC)
RATIO = {"start": "0.05", "hard_min": "0.01", "hard_max": "0.5"}
COUNT = {"start": "10", "hard_min": "2", "hard_max": "100"}


def toml(overrides: dict[str, dict[str, str] | None] | None = None, extra: str = "") -> str:
    lines = []
    for spec in SPECS:
        entry: dict[str, str] | None = RATIO if spec.kind is Kind.RATIO else COUNT
        if overrides and spec.name in overrides:
            entry = overrides[spec.name]
        if entry is None:
            continue
        lines.append(f"[parameters.{spec.name}]")
        lines += [f"{k} = {v}" for k, v in entry.items()]
    return "\n".join(lines) + "\n" + extra


def load(tmp_path: Path, text: str) -> ParameterConfig:
    path = tmp_path / "parameters.toml"
    path.write_text(text, encoding="utf-8")
    return parse_parameters(load_toml(path, name="risk-parameters"))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def repo(tmp_path: Path) -> LedgerRepository:
    upgrade(tmp_path / "ledger.db")
    return LedgerRepository(open_ledger(tmp_path / "ledger.db"))


def store(repo: LedgerRepository, config: ParameterConfig) -> ParameterStore:
    return ParameterStore(repo, config, clock=Clock())


# ---------- 加载（失败即关闭） ----------


def test_loads_every_parameter(tmp_path: Path) -> None:
    config = load(tmp_path, toml())
    assert set(config.bounds) == {s.name for s in SPECS}
    assert config.bounds["max_open_orders"].start == Decimal(10)


@pytest.mark.parametrize(
    ("overrides", "extra"),
    [
        ({"max_open_orders": None}, ""),  # 缺参数
        ({}, "[parameters.allow_short]\nstart = 1\nhard_min = 1\nhard_max = 1\n"),  # 开关不是参数
        ({"max_open_orders": {"start": "10", "hard_min": "2"}}, ""),  # 缺上限
        ({"max_open_orders": {**COUNT, "default": "3"}}, ""),  # 多余键
        ({"max_open_orders": {"start": "1", "hard_min": "2", "hard_max": "100"}}, ""),  # 起点越界
        ({"max_open_orders": {"start": "10.5", "hard_min": "2", "hard_max": "100"}}, ""),  # 非整数
        ({"max_order_notional_nav": {"start": "1.5", "hard_min": "0.01", "hard_max": "2"}}, ""),
        ({"max_order_notional_nav": {"start": "true", "hard_min": "0.01", "hard_max": "0.5"}}, ""),
    ],
)
def test_invalid_config_is_refused(tmp_path: Path, overrides: dict[str, Any], extra: str) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, toml(overrides, extra))


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        parse_parameters(load_toml(tmp_path / "nope.toml", name="risk-parameters"))


# ---------- 调严立即生效、调松须二次确认 ----------


def test_tighten_takes_effect_immediately_and_is_recorded(
    tmp_path: Path, repo: LedgerRepository
) -> None:
    s = store(repo, load(tmp_path, toml()))
    params = s.tighten("max_order_notional_nav", Decimal("0.03"), actor="dylan", reason="降低单笔")
    assert params["max_order_notional_nav"] == Decimal("0.03")
    (entry,) = s.history("max_order_notional_nav")
    assert (entry["kind"], entry["old"], entry["new"], entry["actor"], entry["reason"]) == (
        "tightened", "0.05", "0.03", "dylan", "降低单笔",
    )  # fmt: skip


def test_direction_is_per_parameter(tmp_path: Path, repo: LedgerRepository) -> None:
    s = store(repo, load(tmp_path, toml()))
    # 现金缓冲：数值越大越严；冷却期：越长越严
    assert s.tighten("min_cash_buffer_nav", Decimal("0.2"), actor="a", reason="r")[
        "min_cash_buffer_nav"
    ] == Decimal("0.2")
    with pytest.raises(ParameterError, match="不是调严"):
        s.tighten("stop_cooldown_trading_days", Decimal(5), actor="a", reason="r")
    with pytest.raises(ParameterError, match="不是调严"):
        s.tighten("max_open_orders", Decimal(20), actor="a", reason="r")


def test_loosen_needs_confirmation_with_exact_phrase(
    tmp_path: Path, repo: LedgerRepository
) -> None:
    s = store(repo, load(tmp_path, toml()))
    with pytest.raises(ParameterError, match="不是调松"):
        s.request_loosen("max_open_orders", Decimal(5), actor="a", reason="r")
    pending = s.request_loosen("max_open_orders", Decimal(20), actor="dylan", reason="加单")
    assert s.current()["max_open_orders"] == Decimal(10)  # 请求不生效
    assert pending.confirmation == "确认放宽 max_open_orders 从 10 到 20"
    with pytest.raises(ParameterError, match="短语"):
        s.confirm_loosen(pending.request_id, actor="dylan", confirmation="确认")
    params = s.confirm_loosen(pending.request_id, actor="dylan", confirmation=pending.confirmation)
    assert params["max_open_orders"] == Decimal(20)
    with pytest.raises(ParameterError, match="没有这个"):  # 不能确认两次
        s.confirm_loosen(pending.request_id, actor="dylan", confirmation=pending.confirmation)
    kinds = [e["kind"] for e in s.history("max_open_orders")]
    assert kinds == ["loosen_requested", "loosen_confirmed"]


def test_confirmation_refused_if_value_changed_since_request(
    tmp_path: Path, repo: LedgerRepository
) -> None:
    s = store(repo, load(tmp_path, toml()))
    pending = s.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    s.tighten("max_open_orders", Decimal(5), actor="a", reason="收紧")
    with pytest.raises(ParameterError, match="已经变化"):
        s.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    assert s.current()["max_open_orders"] == Decimal(5)


def test_cancelled_request_cannot_be_confirmed(tmp_path: Path, repo: LedgerRepository) -> None:
    s = store(repo, load(tmp_path, toml()))
    pending = s.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    s.cancel_loosen(pending.request_id, actor="a")
    assert s.pending() == []
    with pytest.raises(ParameterError):
        s.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)


@pytest.mark.parametrize(
    ("name", "value", "fragment"),
    [
        ("max_open_orders", Decimal(1), "上下限"),  # 低于 hard_min
        ("max_open_orders", Decimal(101), "上下限"),  # 高于 hard_max
        ("max_open_orders", Decimal("3.5"), "正整数"),
        ("max_order_notional_nav", Decimal("1.5"), "比例"),
        ("allow_short", Decimal(1), "开关类"),  # 开关不在面板
        ("max_open_orders", Decimal(10), "没有变化"),
    ],
)
def test_adjustments_outside_rules_are_refused(
    tmp_path: Path, repo: LedgerRepository, name: str, value: Decimal, fragment: str
) -> None:
    s = store(repo, load(tmp_path, toml()))
    for adjust in (s.tighten, s.request_loosen):
        with pytest.raises(ParameterError, match=fragment):
            adjust(name, value, actor="a", reason="r")
    assert s.history() == []


def test_actor_and_reason_are_required(tmp_path: Path, repo: LedgerRepository) -> None:
    s = store(repo, load(tmp_path, toml()))
    with pytest.raises(ParameterError, match="操作者与原因"):
        s.tighten("max_open_orders", Decimal(5), actor="a", reason=" ")


# ---------- 重启以保存值为准；越界取更严一端 ----------


def test_saved_values_survive_restart(tmp_path: Path, repo: LedgerRepository) -> None:
    config = load(tmp_path, toml())
    s = store(repo, config)
    s.tighten("max_open_orders", Decimal(5), actor="a", reason="r")
    pending = s.request_loosen("max_daily_loss_nav", Decimal("0.1"), actor="a", reason="r")
    restarted = store(repo, config)
    assert restarted.current()["max_open_orders"] == Decimal(5)
    assert restarted.current()["max_daily_loss_nav"] == Decimal("0.05")  # 未确认的不生效
    assert [p.request_id for p in restarted.pending()] == [pending.request_id]


def test_saved_value_outside_new_bounds_takes_the_stricter_end(
    tmp_path: Path, repo: LedgerRepository
) -> None:
    s = store(repo, load(tmp_path, toml()))
    s.tighten("max_open_orders", Decimal(3), actor="a", reason="r")
    s.tighten("min_cash_buffer_nav", Decimal("0.2"), actor="a", reason="r")
    narrowed = load(
        tmp_path, toml({"max_open_orders": {"start": "10", "hard_min": "5", "hard_max": "50"}})
    )
    assert store(repo, narrowed).current()["max_open_orders"] == Decimal(5)
    buffer = load(
        tmp_path,
        toml({"min_cash_buffer_nav": {"start": "0.03", "hard_min": "0.01", "hard_max": "0.04"}}),
    )
    s2 = store(repo, buffer)
    assert s2.current()["min_cash_buffer_nav"] == Decimal("0.04")  # 越界：现金缓冲取更大（更严）


# ---------- 规则集来源 ----------


def test_rule_source_follows_current_parameters(tmp_path: Path, repo: LedgerRepository) -> None:
    assert rule_source(None)() is None  # 没有参数：拒绝一切新增风险
    s = store(repo, load(tmp_path, toml()))
    source = rule_source(s)
    before = source()
    assert before is not None
    s.tighten("protective_stop_distance", Decimal("0.03"), actor="a", reason="r")
    after = source()
    assert after is not None and after.version != before.version  # 调严立即进入规则集
    assert build_rule_set(s.current()).version == after.version


def test_proposed_template_loads_and_covers_every_parameter() -> None:
    """建议值文件必须能原样加载（Dylan 复制即可用），且参数与定义一一对应。"""
    path = Path(__file__).parents[3] / "docs" / "runbooks" / "risk-parameters.template.toml"
    names = set(tomllib.loads(path.read_text(encoding="utf-8"))["parameters"])
    assert names == {s.name for s in SPECS}
    config = parse_parameters(load_toml(path, name="risk-parameters"))
    # 附录 A 的硬性要求：不加杠杆、止损后冷却不少于 5 个交易日
    assert config.bounds["max_total_exposure_nav"].hard_max <= 1
    assert config.bounds["stop_cooldown_trading_days"].hard_min >= 5


# ---------- R-02a 评审补充 ----------


@pytest.mark.parametrize(
    "overrides",
    [
        {"protective_stop_distance": {"start": "1", "hard_min": "0.01", "hard_max": "1"}},
        {"max_open_orders": {"start": "10", "hard_min": "2", "hard_max": "inf"}},
        {"max_order_notional_nav": {"start": "nan", "hard_min": "0.01", "hard_max": "0.5"}},
        {
            "max_order_notional_nav": {
                "start": "0.0500000000001",
                "hard_min": "0.01",
                "hard_max": "0.5",
            }
        },
    ],
)
def test_degenerate_values_are_refused(tmp_path: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path, toml(overrides))


def test_stop_distance_cannot_be_loosened_to_one(tmp_path: Path, repo: LedgerRepository) -> None:
    config = load(
        tmp_path,
        toml(
            {"protective_stop_distance": {"start": "0.1", "hard_min": "0.01", "hard_max": "0.99"}}
        ),
    )
    with pytest.raises(ParameterError):
        store(repo, config).request_loosen(
            "protective_stop_distance", Decimal(1), actor="a", reason="r"
        )


def forge(repo: LedgerRepository, event_id: str, **detail: str) -> None:

    repo.append_audit(
        AuditEvent(
            event_id=event_id,
            occurred_at=T0,
            actor="intruder",
            action="risk_param_change",
            correlation_id="x",
            detail=tuple(sorted(detail.items())),
        )
    )


def test_forged_records_are_ignored(tmp_path: Path, repo: LedgerRepository) -> None:
    config = load(tmp_path, toml())
    sha = config.sha256
    # 没有对应请求的“已确认调松”
    forge(repo, "f1", kind="loosen_confirmed", name="max_open_orders", old="10", new="100",
          request_id="nope", reason="r", config_sha256=sha)  # fmt: skip
    # 伪装成调严的调松
    forge(repo, "f2", kind="tightened", name="max_daily_loss_nav", old="0.05", new="0.5",
          reason="r", config_sha256=sha)  # fmt: skip
    # 未知参数与缺字段
    forge(repo, "f3", kind="tightened", name="allow_short", old="0", new="1")
    forge(repo, "f4", kind="loosen_confirmed")
    s = store(repo, config)
    current = s.current()
    assert current["max_open_orders"] == Decimal(10)
    assert current["max_daily_loss_nav"] == Decimal("0.05")
    assert s.pending() == []


def test_confirmation_must_match_its_request(tmp_path: Path, repo: LedgerRepository) -> None:
    config = load(tmp_path, toml())
    s = store(repo, config)
    pending = s.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    # 伪造的确认把新值改成 100：与请求不一致，不采用
    forge(repo, f"riskparam-close-{pending.request_id}", kind="loosen_confirmed",
          name="max_open_orders", old="10", new="100", request_id=pending.request_id,
          reason="r", config_sha256=config.sha256)  # fmt: skip
    assert s.current()["max_open_orders"] == Decimal(10)
    with pytest.raises(ParameterError):  # 请求已被（伪造记录）关闭，不能再确认
        s.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)


def test_concurrent_double_confirmation_is_refused(tmp_path: Path, repo: LedgerRepository) -> None:
    config = load(tmp_path, toml())
    first, second = store(repo, config), store(repo, config)
    pending = first.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    first.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    with pytest.raises(ParameterError):
        second.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    with pytest.raises(ParameterError):  # 确认之后不能再取消
        second.cancel_loosen(pending.request_id, actor="a")
    assert [e["kind"] for e in first.history("max_open_orders")] == [
        "loosen_requested",
        "loosen_confirmed",
    ]


def test_config_change_voids_pending_and_confirmed_loosening(
    tmp_path: Path, repo: LedgerRepository
) -> None:
    old = load(tmp_path, toml())
    s = store(repo, old)
    done = s.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    s.confirm_loosen(done.request_id, actor="a", confirmation=done.confirmation)
    waiting = s.request_loosen("max_daily_loss_nav", Decimal("0.1"), actor="a", reason="r")
    new = load(
        tmp_path, toml({"max_open_orders": {"start": "10", "hard_min": "2", "hard_max": "99"}})
    )
    s2 = store(repo, new)
    assert s2.current()["max_open_orders"] == Decimal(10)  # 换版后确认过的调松作废
    assert s2.pending() == []
    with pytest.raises(ParameterError):
        s2.confirm_loosen(waiting.request_id, actor="a", confirmation=waiting.confirmation)


def test_rule_set_uses_the_current_stop_distance(tmp_path: Path, repo: LedgerRepository) -> None:
    s = store(repo, load(tmp_path, toml({
        "protective_stop_distance": {"start": "0.1", "hard_min": "0.01", "hard_max": "0.5"},
    })))  # fmt: skip
    stop = OrderIntent(
        intent_id="stop", created_at=T0, strategy_id=PROTECTIVE_STOP_SOURCE, strategy_version="1",
        account_id=ACCOUNT.account_id, symbol="SPY", side=Side.SELL, quantity=Decimal(100),
        order_type=OrderType.STOP, stop_price=Decimal(92), order_class=OrderClass.PROTECTIVE_STOP,
        time_in_force=TimeInForce.GTC,
    )  # fmt: skip
    context = RiskContext(
        now=T0,
        positions=(
            Position(
                account_id=ACCOUNT.account_id,
                symbol="SPY",
                quantity=Decimal(100),
                average_cost=Decimal(100),
                as_of=T0,
            ),
        ),
        open_orders=(),
        instruments={"SPY": SPY},
        last_prices={"SPY": Decimal(98)},
    )

    def verdict() -> object:
        rule_set = rule_source(s)()
        assert rule_set is not None
        return rule_set.rules[0].check(Subject.new_order(stop), context)

    assert verdict() is None  # 止损 92 在 10% 距离内
    s.tighten("protective_stop_distance", Decimal("0.05"), actor="a", reason="收紧")
    assert verdict() is not None  # 收紧到 5% 后同一张止损被拒


@pytest.mark.parametrize("value", [Decimal("Infinity"), Decimal("NaN"), Decimal("-Infinity")])
def test_non_finite_adjustments_are_refused(
    tmp_path: Path, repo: LedgerRepository, value: Decimal
) -> None:
    s = store(repo, load(tmp_path, toml()))
    for adjust in (s.tighten, s.request_loosen):
        with pytest.raises(ParameterError):
            adjust("max_open_orders", value, actor="a", reason="r")
    assert s.history() == []


def test_racing_confirmations_write_only_once(
    tmp_path: Path, repo: LedgerRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load(tmp_path, toml())
    first, second = store(repo, config), store(repo, config)
    pending = first.request_loosen("max_open_orders", Decimal(20), actor="a", reason="r")
    stale = second._replay()  # 第二个进程在第一个确认之前读到的状态
    first.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    monkeypatch.setattr(second, "_replay", lambda: stale)
    with pytest.raises(ParameterError, match="处理过"):  # 唯一事件号挡住第二次写入
        second.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    closes = [e for e in first.history("max_open_orders") if e["kind"] == "loosen_confirmed"]
    assert len(closes) == 1


@pytest.mark.parametrize(
    ("old", "new", "phrase"),
    [
        ("10", "100", "确认"),  # 短语不是规范短语
        ("10", "5", "确认放宽 max_open_orders 从 10 到 5"),  # 方向是调严，冒充调松
        ("10", "1000", "确认放宽 max_open_orders 从 10 到 1000"),  # 超出上下限
        ("7", "20", "确认放宽 max_open_orders 从 7 到 20"),  # 旧值不是当时的生效值
    ],
)
def test_crude_forged_request_and_confirmation_pair_is_ignored(
    tmp_path: Path, repo: LedgerRepository, old: str, new: str, phrase: str
) -> None:
    # 纵深防御：粗糙的成对伪造在重放时不被承认（真正的边界是写权限与 import 契约）
    config = load(tmp_path, toml())
    common = {"name": "max_open_orders", "old": old, "new": new, "request_id": "rid",
              "reason": "r", "config_sha256": config.sha256}  # fmt: skip
    forge(repo, "p1", kind="loosen_requested", confirmation=phrase, **common)
    forge(repo, "p2", kind="loosen_confirmed", **common)
    assert store(repo, config).current()["max_open_orders"] == Decimal(10)


def test_trailing_zeros_do_not_count_as_places(tmp_path: Path) -> None:
    padded = {"start": "0.0500000000000000", "hard_min": "0.01", "hard_max": "0.5"}
    config = load(tmp_path, toml({"max_order_notional_nav": padded}))
    assert config.bounds["max_order_notional_nav"].start == Decimal("0.05")


@pytest.mark.parametrize(
    ("name", "saved", "narrowed", "clamped", "looser"),
    [
        # 上限类：保存值 3 低于新 hard_min 5，生效 5；之后仍能调松到 10
        ("max_open_orders", "3", {"start": "10", "hard_min": "5", "hard_max": "50"}, "5", "10"),
        # 下限类（现金缓冲越大越严）：保存值 0.2 高于新 hard_max 0.04，生效 0.04；仍能调松到 0.03
        (
            "min_cash_buffer_nav", "0.2",
            {"start": "0.03", "hard_min": "0.01", "hard_max": "0.04"}, "0.04", "0.03",
        ),
    ],
)  # fmt: skip
def test_clamped_value_can_still_be_loosened(  # noqa: PLR0917
    tmp_path: Path,
    repo: LedgerRepository,
    name: str,
    saved: str,
    narrowed: dict[str, str],
    clamped: str,
    looser: str,
) -> None:
    store(repo, load(tmp_path, toml())).tighten(name, Decimal(saved), actor="a", reason="r")
    s = store(repo, load(tmp_path, toml({name: narrowed})))
    assert s.current()[name] == Decimal(clamped)
    pending = s.request_loosen(name, Decimal(looser), actor="a", reason="r")
    assert [p.request_id for p in s.pending()] == [pending.request_id]
    params = s.confirm_loosen(pending.request_id, actor="a", confirmation=pending.confirmation)
    assert params[name] == Decimal(looser)
