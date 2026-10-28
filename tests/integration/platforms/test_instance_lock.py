"""S-01c：单实例锁（真实平台实现：本机 Windows，CI 上 Linux；macOS 明确未实现）。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from server.platforms import PlatformNotImplementedError
from server.platforms.base import PlatformError
from server.platforms.files import InstanceLock, InstanceLockedError


def _supported(tmp_path: Path) -> bool:
    try:
        with InstanceLock(tmp_path / "probe.lock"):
            return True
    except PlatformNotImplementedError:
        return False


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    if not _supported(tmp_path):
        pytest.skip("当前平台的单实例锁尚未实现")
    return tmp_path / "run" / "tradingd.lock"


def test_second_holder_is_refused(lock_path: Path) -> None:
    with InstanceLock(lock_path) as first:
        held_inside = first.held
        with pytest.raises(InstanceLockedError, match="已被另一个实例持有"):
            InstanceLock(lock_path).acquire()
    assert held_inside
    assert not first.held
    with InstanceLock(lock_path):  # 释放后可以重新拿到
        pass


def test_reacquire_on_the_same_object_is_refused(lock_path: Path) -> None:
    lock = InstanceLock(lock_path).acquire()
    try:
        with pytest.raises(PlatformError, match="重复获取"):
            lock.acquire()
    finally:
        lock.release()
    lock.release()  # 重复释放无害


CHILD = """
import sys, time
from pathlib import Path
from server.platforms.files import InstanceLock
lock = InstanceLock(Path(sys.argv[1])).acquire()
print("locked", flush=True)
time.sleep(60)
"""


@pytest.mark.asyncio
async def test_lock_is_held_across_processes_and_freed_when_holder_dies(lock_path: Path) -> None:
    """另一个进程持锁时拿不到；持锁进程被杀（模拟崩溃）后操作系统自动释放。"""
    root = Path(__file__).resolve().parents[3]
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CHILD, str(lock_path),
        stdout=asyncio.subprocess.PIPE, cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
    )  # fmt: skip
    try:
        assert child.stdout is not None
        assert (await asyncio.wait_for(child.stdout.readline(), 30)).strip() == b"locked"
        with pytest.raises(InstanceLockedError):
            InstanceLock(lock_path).acquire()
    finally:
        child.kill()
        await child.wait()
    for _ in range(50):  # Windows 上句柄关闭可能稍晚于进程退出
        try:
            InstanceLock(lock_path).acquire().release()
            break
        except InstanceLockedError:
            await asyncio.sleep(0.1)
    else:
        pytest.fail("持锁进程退出后锁没有被释放")


def test_private_dir_is_created_with_parents(tmp_path: Path) -> None:
    from server.platforms.files import PRIVATE_DIR_MODE, ensure_private_dir  # noqa: PLC0415

    target = tmp_path / "a" / "b" / "run"
    assert ensure_private_dir(target) == target and target.is_dir()
    ensure_private_dir(target)  # 已存在：幂等
    if os.name != "nt":
        import stat  # noqa: PLC0415

        assert stat.S_IMODE(target.stat().st_mode) == PRIVATE_DIR_MODE


def test_private_dir_refuses_a_file_in_the_way(tmp_path: Path) -> None:
    from server.platforms.files import ensure_private_dir  # noqa: PLC0415

    blocker = tmp_path / "run"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(PlatformError):
        ensure_private_dir(blocker)
