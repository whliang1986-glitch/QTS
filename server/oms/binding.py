"""风控决定与评估对象的绑定（R-01，G3 前硬前置；O-03 移交）。

风控对“哪一笔订单”给出结论，就把那笔订单的规范化内容哈希写进决定（RiskDecision.subject_digest）；
OMS 签发与改单时按同样规则重新计算并核对，对不上就拒绝。这样一个 ALLOW 不能被拿去批准
别的意图、别的价格或别的数量。

- 新单：意图的全部字段（数值去掉末尾 0、时间统一 UTC ISO 格式、枚举取值）；
- 改单：意图摘要 + amendment_id + 新价格 + 新数量。

放在 oms 里而不是 domain：哈希需要 hashlib，领域层只允许纯计算模块；risk 可以导入 oms。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import Enum

from server.domain.orders import OrderIntent

DIGEST_VERSION = "1"  # 规范化规则一旦改变就换版本，旧决定自然对不上（失败即关闭）


def _decimal_text(value: Decimal) -> str:
    """去掉末尾 0 的定点文本。固定的高精度上下文：不受调用方 decimal 精度与舍入设置影响。"""
    if value.is_zero():
        return "0"
    with localcontext() as ctx:
        ctx.prec = 60
        text = format(value.normalize(), "f")
    if Decimal(text) != value:
        raise ValueError(f"数值规范化改变了取值：{value}")
    return text


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:  # pragma: no cover - 领域模型已强制带时区
            raise ValueError("时间必须带时区")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _digest(payload: dict[str, object]) -> str:
    text = json.dumps(
        _plain({"v": DIGEST_VERSION, **payload}), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def intent_digest(intent: OrderIntent) -> str:
    """新单的评估对象：意图的全部内容。"""
    return _digest({"kind": "intent", "intent": intent.model_dump()})


def amendment_digest(
    intent: OrderIntent, *, amendment_id: str, new_price: Decimal, new_quantity: Decimal
) -> str:
    """改单的评估对象：原意图 + 改单幂等键 + 新价格 + 新数量。"""
    return _digest(
        {
            "kind": "amendment",
            "intent": intent_digest(intent),
            "amendment_id": amendment_id,
            "new_price": new_price,
            "new_quantity": new_quantity,
        }
    )
