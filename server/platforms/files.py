"""文件与目录的平台差异（S-01b 起：业务代码不得自己判断操作系统）。

- lock_directory：把目录设为只读（数据湖分区封存）。
- InstanceLock：单实例锁（S-01c）。同一把锁同一时刻只能被一个进程持有，
  用来保证 tradingd 是账本的唯一写者、guardd / workerd 不会起两份。
  进程退出（含崩溃）时操作系统自动释放，不会留下“死锁文件”。
  Windows 用 msvcrt.locking，Linux 用 fcntl.flock；macOS 尚未实现（P-MAC），明确报错。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import TracebackType

from server.platforms.base import PlatformError, PlatformNotImplementedError


def lock_directory(directory: Path) -> bool:
    """把目录设为只读、禁止新增与删除条目；返回是否真的生效。

    POSIX（Linux、macOS）上去掉目录写权限即可阻止删除与新增；Windows 的目录只读属性不阻止删除，
    这里不做任何改动并返回 False，调用方不得把它当成已上锁。
    """
    if os.name == "nt":
        return False
    directory.chmod(stat.S_IREAD | stat.S_IEXEC | stat.S_IRGRP | stat.S_IXGRP)
    return True


class InstanceLockedError(PlatformError):
    """锁已被另一个实例持有。"""


if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # 锁第 0 个字节；已被锁时立即抛 OSError

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

elif sys.platform == "linux":
    import fcntl

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

else:

    def _try_lock(fd: int) -> None:
        raise PlatformNotImplementedError(f"{sys.platform} 的单实例锁尚未实现（P-MAC）")

    def _unlock(fd: int) -> None:
        raise PlatformNotImplementedError(f"{sys.platform} 的单实例锁尚未实现（P-MAC）")


class InstanceLock:
    """单实例锁：with InstanceLock(path): ...；拿不到锁抛 InstanceLockedError。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> InstanceLock:
        if self._fd is not None:
            raise PlatformError("同一个锁对象不能重复获取")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _try_lock(fd)
        except OSError as exc:
            os.close(fd)
            raise InstanceLockedError(f"{self._path} 已被另一个实例持有") from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


__all__ = ("InstanceLock", "InstanceLockedError", "lock_directory")
