"""tradingd 心跳文件（S-01c）：tradingd 定时写，guardd 读。

- 内容是一行 UTC ISO 时间；先写临时文件再 os.replace 原子替换，读者不会读到半行。
- Windows 上读者恰好打开着文件时替换可能失败（PermissionError）：短暂重试，仍失败就抛出；
  tradingd 捕获后记日志、跳过这一次。少写一次心跳只会让 guardd 更早介入（偏保守），不会掩盖故障。
- 读：文件缺失、内容坏、不带时区一律当作“没有心跳”（失败即关闭：guardd 按心跳丢失处理）。
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import datetime
from pathlib import Path

_REPLACE_RETRIES = 5


class HeartbeatFile:
    def __init__(self, path: Path) -> None:
        self._path = path

    def beat(self, at: datetime) -> None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("心跳时间必须带时区")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + f".{os.getpid()}.tmp")
        tmp.write_text(at.isoformat(), encoding="utf-8")
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    raise
                time.sleep(0.02)

    def last(self) -> datetime | None:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
            value = datetime.fromisoformat(text)
        except (OSError, ValueError):
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value


__all__ = ("HeartbeatFile",)
