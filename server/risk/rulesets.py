"""按当前生效的风控参数组装规则集（R-02）。

`ParameterStore.current()` 每次评估现取，所以面板上的调严立即生效（ADR-16）；参数加载失败或缺失时
返回 None，引擎拒绝一切新增风险（rule_set_missing）。
规则集版本 = 参数版本（配置内容版本 + 已生效调整数），写入每个风控决定，
审计时能知道按哪一版参数放行。

R-02b 在这里加入阈值规则（单笔、单标的、总敞口、现金缓冲、日内亏损、未成交订单数、
频率、限价偏离、冷却期）。
"""

from __future__ import annotations

from collections.abc import Callable

from server.risk.default_deny import DefaultDenyRule, ProtectiveStopParams
from server.risk.engine import RuleSet
from server.risk.limits import limit_rules
from server.risk.parameters import ParameterStore, RiskParameters


def build_rule_set(params: RiskParameters) -> RuleSet:
    protective = ProtectiveStopParams(stop_distance=params["protective_stop_distance"])
    return RuleSet(
        version=f"params:{params.version}",
        rules=(DefaultDenyRule(protective), *limit_rules(params)),
    )


def rule_source(store: ParameterStore | None) -> Callable[[], RuleSet | None]:
    """交给 RiskEngine 的规则来源：没有参数（受保护配置未提交或不合法）时返回 None。"""

    def current() -> RuleSet | None:
        return None if store is None else build_rule_set(store.current())

    return current
