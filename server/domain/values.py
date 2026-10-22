"""基础值类型：Decimal 金额与数量、UTC 时间、领域模型基类（C-01）。

- 价格、数量、金额一律是 Decimal：拒绝 float 与 bool（含 JSON 里带小数的数字），
  拒绝 NaN / Infinity。
- 时间必须带时区，统一换算为 UTC（ADR-12）。
- 模型 frozen、未知字段拒绝；修改只能用 evolve()，会重新校验。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

# 数值范围：最多 10 位小数、整数部分不超过 15 位；超出视为脏数据
# （也防止 1e999999999 这类值拖垮后续运算）
_MAX_FRACTION_DIGITS = 10
_MAX_INTEGER_DIGITS = 15
_NUMERIC_TEXT = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool | float):
        raise ValueError("金额与数量不得使用 float 或 bool，请用 Decimal 或字符串")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        # 只接受规范写法：拒绝空格、下划线、NaN/Infinity 等 Decimal 构造器宽容接受的形式
        if not _NUMERIC_TEXT.fullmatch(value):  # fullmatch：末尾换行也拒绝；只认 ASCII 数字
            raise ValueError(f"无法解析为 Decimal：{value!r}")
        try:
            result = Decimal(value)
        except InvalidOperation as exc:  # pragma: no cover - 正则已拦截
            raise ValueError(f"无法解析为 Decimal：{value!r}") from exc
    else:
        raise ValueError(f"不支持的数值类型：{type(value).__name__}")
    if not result.is_finite():
        raise ValueError("金额与数量必须是有限值")
    exponent = result.as_tuple().exponent
    if not isinstance(exponent, int):  # 有限值的指数一定是 int；防御性检查
        raise ValueError("金额与数量必须是有限值")
    if -exponent > _MAX_FRACTION_DIGITS or result.adjusted() >= _MAX_INTEGER_DIGITS:
        raise ValueError(
            f"超出允许范围：最多 {_MAX_FRACTION_DIGITS} 位小数、{_MAX_INTEGER_DIGITS} 位整数"
        )
    return result


def _positive(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("必须大于 0")
    return value


def _non_negative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("不得为负")
    return value


def _whole(value: Decimal) -> Decimal:
    if value != value.to_integral_value():
        raise ValueError("首期只交易整股，数量必须是整数")
    return value


def to_utc(value: object) -> datetime:
    """把带时区的时间规范为纯 UTC datetime；不带时区或无法解析时抛 ValueError。"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{value!r}") from exc
    if not isinstance(value, datetime):
        raise ValueError("必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区（内部一律 UTC）")
    utc = value.astimezone(UTC)
    # 重建为纯 datetime：不让 datetime 子类（可能改写比较或格式化行为）流入领域对象
    return datetime(
        utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, utc.microsecond, tzinfo=UTC
    )


_DecimalStr = PlainSerializer(str, return_type=str, when_used="json")

Money = Annotated[Decimal, BeforeValidator(_to_decimal), _DecimalStr]
Price = Annotated[Decimal, BeforeValidator(_to_decimal), AfterValidator(_positive), _DecimalStr]
Quantity = Annotated[
    Decimal, BeforeValidator(_to_decimal), AfterValidator(_non_negative), _DecimalStr
]
PositiveQuantity = Annotated[
    Decimal, BeforeValidator(_to_decimal), AfterValidator(_positive), _DecimalStr
]
# 订单与成交数量：正整数（首期整股）
ShareQuantity = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    AfterValidator(_positive),
    AfterValidator(_whole),
    _DecimalStr,
]
# 带符号数量：只用于记录券商事实（例如持仓），负值必须由对账与风控当作异常处理
SignedQuantity = Annotated[Decimal, BeforeValidator(_to_decimal), _DecimalStr]
UtcDatetime = Annotated[datetime, BeforeValidator(to_utc)]
Currency = Literal["USD"]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]


class ValueModel(BaseModel):
    """领域模型基类：只读、拒绝未知字段；修改只能走 evolve() 并重新校验。

    所有绕过校验的入口都被关闭：model_construct、copy(update=...)、model_copy(update=...)；
    嵌套或传入的已有实例也一律重新校验（revalidate_instances="always"）。
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", validate_default=True, revalidate_instances="always"
    )

    # 允许通过 evolve() 修改的字段。默认完全不可修改（失败即关闭），需要修改的模型自己声明；
    # None 表示全部字段都可修改。必须是 ClassVar：否则 pydantic 会把它当成可在实例上改写的私有属性。
    evolvable_fields: ClassVar[frozenset[str] | None] = frozenset()

    @classmethod
    # pydantic 的 mypy 插件会按字段合成 model_construct 签名，覆盖时签名必然不同
    def model_construct(  # type: ignore[override]
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        raise TypeError("model_construct 会跳过校验，领域模型禁止使用")

    def copy(self, *args: Any, **kwargs: Any) -> Self:
        raise TypeError("copy() 已弃用且不做校验，领域模型禁止使用")

    def model_copy(self, *, update: Any = None, deep: bool = False) -> Self:
        if update:
            raise TypeError("model_copy(update=...) 会绕过校验；请使用 evolve()")
        return super().model_copy(deep=deep)

    def evolve(self, **changes: Any) -> Self:
        """返回修改后的新对象；会完整重新校验，只允许修改 evolvable_fields 中的字段。"""
        allowed = type(self).evolvable_fields
        if allowed is not None:
            frozen = set(changes) - allowed
            if frozen:
                raise ValueError(f"字段不可修改：{sorted(frozen)}")
        return self.model_validate({**self.model_dump(), **changes})
