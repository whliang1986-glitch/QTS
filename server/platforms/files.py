"""文件与目录权限的平台差异（S-01b 起：业务代码不得自己判断操作系统）。"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def lock_directory(directory: Path) -> bool:
    """把目录设为只读、禁止新增与删除条目；返回是否真的生效。

    POSIX（Linux、macOS）上去掉目录写权限即可阻止删除与新增；Windows 的目录只读属性不阻止删除，
    这里不做任何改动并返回 False，调用方不得把它当成已上锁。
    """
    if os.name == "nt":
        return False
    directory.chmod(stat.S_IREAD | stat.S_IEXEC | stat.S_IRGRP | stat.S_IXGRP)
    return True


__all__ = ("lock_directory",)
