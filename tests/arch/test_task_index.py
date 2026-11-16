"""任务索引与任务单一致：每份任务单都登记在 docs/tasks/README.md，状态与任务单相同。

2026-11 自检发现索引只列了约 30 个任务（实际 80 多份任务单），不少状态也过期了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TASKS = Path(__file__).resolve().parents[2] / "docs" / "tasks"
NOT_TASKS = {"README.md", "TEMPLATE.md"}


def _status(text: str) -> str:
    m = re.search(r"^\| 状态 \|\s*(.*?)\s*\|\s*$", text, re.M)
    assert m is not None, "任务单缺少状态一行"
    return m.group(1)


def _index() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in (TASKS / "README.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| \[([^\]]+)\]\(([^)#]+)\) \|.*\| ([^|]*?) \|$", line)
        if m:
            assert m.group(2) == f"{m.group(1)}.md", f"索引链接与编号不一致：{line}"
            rows[m.group(1)] = m.group(3)
    return rows


TASK_FILES = sorted(p for p in TASKS.glob("*.md") if p.name not in NOT_TASKS)


@pytest.mark.parametrize("path", TASK_FILES, ids=lambda p: p.stem)
def test_every_task_file_is_indexed_with_its_status(path: Path) -> None:
    index = _index()
    assert path.stem in index, f"{path.name} 没有登记在 docs/tasks/README.md"
    assert index[path.stem] == _status(path.read_text(encoding="utf-8")), (
        f"{path.name} 的状态与索引不一致"
    )


def test_index_links_point_to_existing_task_files() -> None:
    missing = sorted(i for i in _index() if not (TASKS / f"{i}.md").exists())
    assert missing == []
