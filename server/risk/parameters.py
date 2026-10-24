"""风控参数与运行时调整（R-02，ADR-16“风控面板”后端）。

- 参数“是什么”在这里定义（名称、单位、类型、哪个方向更松），**数值一个都不在代码里**：
  启动值与硬性上下限（hard_min / hard_max）全部来自 Dylan 提交的受保护配置
  `config/risk/parameters.toml`。
  文件缺失、缺参数、多参数、类型不对、上下限不成立 → 加载失败，风控对一切新增风险拒绝（默认最严）。
- 运行时调整只能在 [hard_min, hard_max] 内：
  - 调严：立即生效，写只追加的变更记录（时间、旧值、新值、操作者、原因）；
  - 调松：先记一条待确认请求；Dylan 用独立的确认操作与确认短语确认后才生效
    （确认时当前值必须仍等于请求时的旧值）。
    确认入口只能在本模块与 server/api 中出现（arch_scan risk-param-loosen），AI 与策略没有确认入口。
- 变更记录写在只追加的审计表（action = risk_param_change）；重启后按记录重放得到当前值。
  保存值超出新配置的上下限时取更严的一端并告警，不报错退出。
- 开关类（杠杆、做空、衍生品、市价单、风控口径）不是参数，不进面板，只能改受保护配置并重启。
  急停（R-05）独立生效。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from server.domain.modes import AuditEvent
from server.storage.config import ConfigError, LoadedConfig
from server.storage.errors import ConflictError
from server.storage.ledger import LedgerRepository
from server.storage.logs import log_event

log = logging.getLogger(__name__)

CHANGE_ACTION = "risk_param_change"


class Kind(StrEnum):
    RATIO = "ratio"  # 0 < x ≤ 1 的比例（占 NAV 的比例等），Decimal
    COUNT = "count"  # 正整数（笔数、毫秒、基点、交易日）


class Looser(StrEnum):
    """哪个方向更松：判定调严 / 调松。"""

    HIGHER = "higher"  # 数值越大越松（上限类）
    LOWER = "lower"  # 数值越小越松（下限类，如现金缓冲、冷却期）


@dataclass(frozen=True)
class ParamSpec:
    name: str
    unit: str
    kind: Kind
    looser: Looser
    level: str
    description: str
    upper_open: bool = False  # 比例的上界不含 1（如止损距离：1 等于不设止损）


# 附录 A 中进入面板的阈值（草案值见附录 A，由 Dylan 在受保护配置中批准；这里不写任何数值）
SPECS: tuple[ParamSpec, ...] = (
    ParamSpec(
        "max_order_notional_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "订单",
        "单笔订单最大名义金额",
    ),
    ParamSpec(
        "max_position_etf_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "标的",
        "ETF 单标的市值上限",
    ),
    ParamSpec(
        "max_position_stk_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "标的",
        "个股单标的市值上限",
    ),
    ParamSpec(
        "max_total_exposure_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "账户",
        "总敞口上限（不含杠杆）",
    ),
    ParamSpec(
        "min_cash_buffer_nav", "NAV 比例", Kind.RATIO, Looser.LOWER, "账户",
        "最低现金缓冲",
    ),
    ParamSpec(
        "max_daily_loss_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "账户",
        "日内亏损上限（触发 HALT）",
    ),
    ParamSpec(
        "max_strategy_drawdown", "比例", Kind.RATIO, Looser.HIGHER, "策略",
        "策略最大回撤",
    ),
    ParamSpec(
        "max_open_orders", "笔", Kind.COUNT, Looser.HIGHER, "订单",
        "最大未成交订单数（不含保护性止损）",
    ),
    ParamSpec(
        "max_orders_per_minute", "笔/分钟", Kind.COUNT, Looser.HIGHER, "订单",
        "下单频率上限",
    ),
    ParamSpec(
        "max_limit_deviation", "比例", Kind.RATIO, Looser.HIGHER, "订单",
        "限价偏离（相对最新价）",
    ),
    ParamSpec(
        "max_quote_age_ms", "毫秒", Kind.COUNT, Looser.HIGHER, "系统",
        "报价最大陈旧时间",
    ),
    ParamSpec(
        "max_spread_bps", "基点", Kind.COUNT, Looser.HIGHER, "标的",
        "最大买卖价差",
    ),
    ParamSpec(
        "max_clock_skew_ms", "毫秒", Kind.COUNT, Looser.HIGHER, "系统",
        "时钟偏差上限",
    ),
    ParamSpec(
        "protective_stop_distance", "比例", Kind.RATIO, Looser.HIGHER, "持仓",
        "保护性止损距离（成本价下方）", True,
    ),
    ParamSpec(
        "stop_cooldown_trading_days", "交易日", Kind.COUNT, Looser.LOWER, "标的",
        "止损后最短冷却期",
    ),
    ParamSpec(
        "smoke_order_nav", "NAV 比例", Kind.RATIO, Looser.HIGHER, "策略",
        "冒烟策略单笔金额（仅模拟盘）",
    ),
)  # fmt: skip
SPEC_BY_NAME: Mapping[str, ParamSpec] = MappingProxyType({s.name: s for s in SPECS})
BOUND_KEYS = frozenset({"start", "hard_min", "hard_max"})


class ParameterError(Exception):
    """调整被拒绝：越界、方向不对、确认不匹配等（失败即关闭，值不变）。"""


@dataclass(frozen=True)
class Bounds:
    start: Decimal
    hard_min: Decimal
    hard_max: Decimal


@dataclass(frozen=True)
class ParameterConfig:
    """受保护配置解析结果：每个参数的启动值与上下限。version 是配置文件的内容版本。"""

    version: str
    sha256: str
    bounds: Mapping[str, Bounds]


@dataclass(frozen=True)
class RiskParameters:
    """某一时刻的生效值快照（规则只读这个）。version 随配置与每次生效的调整变化，写入风控决定。"""

    version: str
    values: Mapping[str, Decimal]

    def __getitem__(self, name: str) -> Decimal:
        return self.values[name]


@dataclass(frozen=True)
class PendingLoosen:
    request_id: str
    name: str
    old: Decimal
    new: Decimal
    confirmation: str  # Dylan 确认时必须逐字输入的短语
    config_sha256: str  # 请求时的受保护配置版本：配置变了请求即作废


MAX_PLACES = 12  # 参数值最多 12 位小数，规范化与落账都不会舍入


def _number(value: Any, spec: ParamSpec, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ConfigError(f"{spec.name}.{field} 必须是数字")
    number = Decimal(value)
    if not number.is_finite():  # inf / nan（TOML 允许）一律拒绝
        raise ConfigError(f"{spec.name}.{field} 必须是有限数值")
    exponent = number.normalize().as_tuple().exponent  # 0.1000 这类末尾补零不算小数位
    if isinstance(exponent, int) and exponent < -MAX_PLACES:
        raise ConfigError(f"{spec.name}.{field} 小数位过多")
    upper_ok = number < 1 if spec.upper_open else number <= 1
    if spec.kind is Kind.RATIO and not (number > 0 and upper_ok):
        bracket = ")" if spec.upper_open else "]"
        raise ConfigError(f"{spec.name}.{field} 必须是 (0, 1{bracket} 之间的比例")
    if spec.kind is Kind.COUNT and (number != number.to_integral_value() or number <= 0):
        raise ConfigError(f"{spec.name}.{field} 必须是正整数")
    return number


def parse_parameters(loaded: LoadedConfig) -> ParameterConfig:
    """解析 `[parameters.<name>]` 表：每个参数恰好 start / hard_min / hard_max 三项，不缺不多。"""
    if loaded.name != "risk-parameters":
        raise ConfigError(f"风控参数只能从名为 risk-parameters 的配置加载（收到 {loaded.name}）")
    table = loaded.only_table("parameters")
    missing = sorted(set(SPEC_BY_NAME) - set(table))
    extra = sorted(set(table) - set(SPEC_BY_NAME))
    if missing or extra:
        raise ConfigError(f"风控参数不完整或有多余项：缺 {missing}，多 {extra}")
    bounds: dict[str, Bounds] = {}
    for name, spec in SPEC_BY_NAME.items():
        entry = table[name]
        if not isinstance(entry, Mapping) or set(entry) != BOUND_KEYS:
            raise ConfigError(f"{name} 必须恰好包含 start、hard_min、hard_max")
        try:
            b = Bounds(*(_number(entry[k], spec, k) for k in ("start", "hard_min", "hard_max")))
        except InvalidOperation as exc:  # 统一成配置错误：启动方按“没有参数”处理
            raise ConfigError(f"{name} 数值不合法") from exc
        if not b.hard_min <= b.start <= b.hard_max:
            raise ConfigError(f"{name} 必须满足 hard_min ≤ start ≤ hard_max")
        bounds[name] = b
    return ParameterConfig(loaded.version, loaded.sha256, MappingProxyType(bounds))


def _text(value: Decimal) -> str:
    """定点文本，去掉末尾 0；在固定高精度上下文里计算，绝不舍入（_number 已限制小数位）。"""
    if value.is_zero():
        return "0"
    with localcontext() as ctx:
        ctx.prec = 60
        return format(value.normalize(), "f")


def _phrase(name: str, old: Decimal, new: Decimal) -> str:
    """Dylan 确认调松时必须逐字输入的短语。"""
    return f"确认放宽 {name} 从 {_text(old)} 到 {_text(new)}"


def _is_looser(spec: ParamSpec, old: Decimal, new: Decimal) -> bool:
    return new > old if spec.looser is Looser.HIGHER else new < old


def _stricter_end(spec: ParamSpec, b: Bounds) -> Decimal:
    return b.hard_min if spec.looser is Looser.HIGHER else b.hard_max


@dataclass
class _Replay:
    values: dict[str, Decimal]
    applied: int
    open_requests: dict[str, PendingLoosen]


class ParameterStore:
    """生效值 = 启动值 + 按顺序重放、逐条核验通过的调整（单写者 tradingd 持有）。

    重放对记录做一致性核验：调严必须真的更严；调松请求必须方向、上下限、短语、配置版本、旧值都对；
    确认必须对应之前同一请求号的合格请求（参数、旧值、新值一致，旧值仍等于当时的生效值）。
    不合格的记录（损坏、缺字段、未知参数、粗糙伪造）跳过并告警，不抛错。

    这只是纵深防御：构造严密的成对伪造（请求 + 确认）重放识别不了，记录是否出自本模块无法证明。
    真正的边界是写权限：只有 tradingd 持有账本写权限（S-01）；ai、strategy 不得依赖 storage 与 risk
    （import-linter）；变更记录的动作名与写入方法只能出现在本模块（arch_scan risk-param-write）。
    """

    def __init__(
        self,
        repo: LedgerRepository,
        config: ParameterConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._repo = repo
        self._config = config
        self._clock = clock
        self._new_id = new_id
        self._warned: set[tuple[str, str]] = set()

    # ---------- 查询 ----------

    def current(self) -> RiskParameters:
        replay = self._replay()  # 重放过程中每一步都已夹进上下限（_clamp）
        return RiskParameters(
            version=f"{self._config.version}+{replay.applied}",
            values=MappingProxyType(dict(replay.values)),
        )

    def _clamp(self, name: str, value: Decimal) -> Decimal:
        """保存值超出当前配置的上下限：取更严的一端并告警（重放与对外的生效值同一口径）。"""
        spec, b = SPEC_BY_NAME[name], self._config.bounds[name]
        if b.hard_min <= value <= b.hard_max:
            return value
        self._warn_once("out_of_bounds", name)
        return _stricter_end(spec, b)

    def pending(self) -> list[PendingLoosen]:
        """仍可确认的调松请求（配置版本变了或已确认、已取消的不在其中）。"""
        return list(self._replay().open_requests.values())

    def history(self, name: str | None = None) -> list[Mapping[str, str]]:
        """变更记录原文（从旧到新），每条含 kind、name、old、new、actor、reason、at。"""
        out: list[Mapping[str, str]] = []
        for event in self._changes():
            d = dict(event.detail)
            if name is None or d.get("name") == name:
                out.append(
                    MappingProxyType(
                        {**d, "actor": event.actor, "at": event.occurred_at.isoformat()}
                    )
                )
        return out

    # ---------- 调整 ----------

    def tighten(self, name: str, value: Decimal, *, actor: str, reason: str) -> RiskParameters:
        """调严：立即生效。必须严格更严，且在上下限内。"""
        spec, old, new = self._check(name, value)
        if not _is_looser(spec, new, old):
            raise ParameterError(f"{name} 从 {_text(old)} 到 {_text(new)} 不是调严")
        self._append_change("tightened", spec.name, old, new, actor, reason)
        return self.current()

    def request_loosen(
        self, name: str, value: Decimal, *, actor: str, reason: str
    ) -> PendingLoosen:
        """调松：只记待确认请求，不生效。"""
        spec, old, new = self._check(name, value)
        if not _is_looser(spec, old, new):
            raise ParameterError(f"{name} 从 {_text(old)} 到 {_text(new)} 不是调松")
        request_id = self._new_id()
        phrase = _phrase(spec.name, old, new)
        self._append_change(
            "loosen_requested", spec.name, old, new, actor, reason,
            request_id=request_id, confirmation=phrase,
        )  # fmt: skip
        return PendingLoosen(request_id, spec.name, old, new, phrase, self._config.sha256)

    def confirm_loosen(self, request_id: str, *, actor: str, confirmation: str) -> RiskParameters:
        """Dylan 二次确认调松：短语一致、请求仍待确认、当前值仍等于请求时的旧值、仍在上下限内。"""
        request = self._replay().open_requests.get(request_id)
        if request is None:
            raise ParameterError("没有这个待确认的调松请求（已确认、已取消、配置已变或不存在）")
        if confirmation != request.confirmation:
            raise ParameterError("确认短语不一致，调松未生效")
        spec, old, new = self._check(request.name, request.new)
        if old != request.old:
            raise ParameterError("参数在请求之后已经变化，请重新发起调松")
        self._append_change(
            "loosen_confirmed", spec.name, old, new, actor, "二次确认",
            request_id=request_id, event=f"close-{request_id}",
        )  # fmt: skip
        current = self.current()
        if current[spec.name] != new:  # 已写入关闭记录，但重放没有采用（并发变化等）
            raise ParameterError("调松请求已关闭但未生效，请检查当前值后重新发起")
        return current

    def cancel_loosen(self, request_id: str, *, actor: str) -> None:
        request = self._replay().open_requests.get(request_id)
        if request is None:
            raise ParameterError("没有这个待确认的调松请求")
        self._append_change(
            "loosen_cancelled", request.name, request.old, request.new, actor, "取消",
            request_id=request_id, event=f"close-{request_id}",
        )  # fmt: skip

    # ---------- 内部 ----------

    def _check(self, name: str, value: Decimal) -> tuple[ParamSpec, Decimal, Decimal]:
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            raise ParameterError(f"没有参数 {name}（开关类不在面板中）")
        try:
            new = _number(value, spec, "value")
        except (ConfigError, InvalidOperation) as exc:
            raise ParameterError(str(exc)) from exc
        b = self._config.bounds[name]
        if not b.hard_min <= new <= b.hard_max:
            raise ParameterError(f"{name} 必须在受保护配置的上下限内")
        old = self.current()[name]
        if new == old:
            raise ParameterError(f"{name} 的值没有变化")
        return spec, old, new

    def _replay(self) -> _Replay:
        state = _Replay({n: b.start for n, b in self._config.bounds.items()}, 0, {})
        closed: set[str] = set()
        for event in self._changes():
            try:
                self._apply(state, closed, dict(event.detail))
            except (KeyError, ValueError, ArithmeticError):
                self._warn_once("malformed", event.event_id)
        return state

    def _apply(self, state: _Replay, closed: set[str], d: dict[str, str]) -> None:
        kind, name = d["kind"], d["name"]
        spec, b = SPEC_BY_NAME[name], self._config.bounds[name]
        old, new = Decimal(d["old"]), Decimal(d["new"])
        current = state.values[name]
        if kind == "tightened":
            # 调严记录只要比当前重放值更严就采用（配置换版后，比新启动值更严的保存值仍然有效）；
            # 采用后立即夹进上下限，之后的请求与确认都和对外的生效值比较
            if _is_looser(spec, new, current):
                state.values[name], state.applied = self._clamp(name, new), state.applied + 1
            else:
                self._warn_once("rejected_record", d.get("request_id", name + d["new"]))
        elif kind == "loosen_requested":
            rid = d["request_id"]
            well_formed = (
                old == current
                and _is_looser(spec, old, new)
                and b.hard_min <= new <= b.hard_max
                and d["confirmation"] == _phrase(name, old, new)
                and d["config_sha256"] == self._config.sha256
            )
            if not well_formed:  # 方向、上下限、短语、配置版本、旧值任一不符：不承认这个请求
                self._warn_once("rejected_record", rid)
            elif rid not in closed and rid not in state.open_requests:
                state.open_requests[rid] = PendingLoosen(
                    rid, name, old, new, d["confirmation"], d["config_sha256"]
                )
        elif kind in {"loosen_confirmed", "loosen_cancelled"}:
            rid = d["request_id"]
            request = state.open_requests.pop(rid, None)
            closed.add(rid)
            valid = (
                request is not None
                and (request.name, request.old, request.new) == (name, old, new)
                and request.config_sha256 == self._config.sha256
            )
            if kind == "loosen_cancelled":
                return
            if valid and old == current and b.hard_min <= new <= b.hard_max:
                state.values[name], state.applied = self._clamp(name, new), state.applied + 1
            else:
                self._warn_once("rejected_record", rid)
        # 配置版本变了的待确认请求作废（不在 open_requests 中）
        state.open_requests = {
            k: v for k, v in state.open_requests.items()
            if v.config_sha256 == self._config.sha256
        }  # fmt: skip

    def _warn_once(self, what: str, key: str) -> None:
        if (what, key) not in self._warned:
            self._warned.add((what, key))
            log_event(log, logging.WARNING, "风控参数记录异常", what=what, key=key)

    def _changes(self) -> list[AuditEvent]:
        return list(reversed(self._repo.audits(CHANGE_ACTION)))  # 从旧到新

    def _append_change(  # noqa: PLR0913, PLR0917 - 变更记录的字段全部显式给出
        self,
        kind: str,
        name: str,
        old: Decimal,
        new: Decimal,
        actor: str,
        reason: str,
        *,
        event: str | None = None,
        **extra: str,
    ) -> None:
        if not actor.strip() or not reason.strip():
            raise ParameterError("必须写明操作者与原因")
        detail = {
            "kind": kind, "name": name, "old": _text(old), "new": _text(new),
            "reason": reason, "config_sha256": self._config.sha256, **extra,
        }  # fmt: skip
        record = AuditEvent(
            # 确认 / 取消用确定的事件号：同一请求只能关闭一次（并发重复确认在唯一约束上失败）
            event_id=f"riskparam-{event or self._new_id()}",
            occurred_at=self._clock(),
            actor=actor,
            action=CHANGE_ACTION,
            correlation_id=name,
            detail=tuple(sorted(detail.items())),
        )
        try:
            written = self._repo.append_audit(record)
        except ConflictError as exc:  # 并发：同一请求已被另一路关闭（唯一事件号）
            raise ParameterError("这个调松请求已经处理过") from exc
        if not written:  # 同一请求已被关闭过（内容完全相同的重放）
            raise ParameterError("这个调松请求已经处理过")
        log_event(log, logging.INFO, "风控参数变更", kind=kind, name=name)
