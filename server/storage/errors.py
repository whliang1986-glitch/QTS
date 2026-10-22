"""存储层错误（C-03）。账本写入失败一律抛出，不吞掉（fail closed）。"""

from __future__ import annotations


class StorageError(Exception):
    """账本读写失败：数据库锁、只读、未迁移、外键或约束违反等。"""


class ConflictError(StorageError):
    """同一个幂等键（intent_id、execution_id、request_id、券商订单号、permId）对应了不同内容。"""
