"""领域错误（C-01）。"""

from __future__ import annotations


class DomainError(Exception):
    """领域规则被违反。"""


class InstrumentSpecMissingError(DomainError):
    """标的规格缺失：禁止据此下单（V4.1 6.1）。"""
