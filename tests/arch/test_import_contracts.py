"""F-03 验收：违反分层依赖规则的 import 会让 lint-imports 失败（CI 随之失败）。

契约写在 pyproject.toml 的 [tool.importlinter]。这里把同一份配置拿到临时包树上，
逐条植入违规 import，确认每条契约都真的会报错，而合规的 import 不会误报。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.arch._layout import make_server_tree

ROOT = Path(__file__).resolve().parents[2]
LINT_IMPORTS = shutil.which("lint-imports", path=str(Path(sys.executable).parent))


def _lint(project: Path) -> subprocess.CompletedProcess[str]:
    assert LINT_IMPORTS is not None, "import-linter 是开发依赖，缺失时必须失败而不是跳过（uv sync）"
    env = {**os.environ, "PYTHONPATH": str(project), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [LINT_IMPORTS, "--config", str(project / "pyproject.toml"), "--no-cache", "--no-logo"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    make_server_tree(tmp_path, files)
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def test_real_repository_passes() -> None:
    result = _lint(ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROKEN" not in result.stdout


DOMAIN = "domain 不得依赖任何其他层"
DECISION = "strategy、ai、api 不得依赖 broker（直接或间接）"
ALLOWLIST = "只有 oms、guard、backtest、data 可以直接 import broker"
NO_LEDGER = "ai、strategy 不得依赖 storage 与 risk（R-02a：不能伪造参数变更或风控决定）"
CHANNEL = "ai、strategy 不得依赖平台层与进程间通道（S-01b：不能绕过 outbox 直连 tradingd）"
LEAF = "平台适配层只依赖标准库，不得依赖任何业务层（S-01b）"
NO_API = "ai、strategy 不得依赖 api（R-02a：不能经 api 的包装函数间接调松风控参数或确认）"

# 植入的文件 → 预期被打破的契约名（断言具体契约，防止被另一条契约顶替着通过）
VIOLATIONS: dict[str, tuple[dict[str, str], str]] = {
    "domain 依赖 oms": ({"server/domain/order.py": "import server.oms\n"}, DOMAIN),
    "domain 依赖 storage": ({"server/domain/x.py": "from server.storage import repo\n"}, DOMAIN),
    "strategy 直接依赖 broker": (
        {"server/strategy/s.py": "from server.broker import fake\n"},
        DECISION,
    ),
    "strategy 经 oms 间接依赖 broker": (
        {
            "server/oms/core.py": "import server.broker\n",
            "server/strategy/s.py": "import server.oms.core\n",
        },
        DECISION,
    ),
    "ai 依赖 broker.ibkr": ({"server/ai/agent.py": "import server.broker.ibkr\n"}, DECISION),
    "api 依赖 broker": ({"server/api/routes.py": "from server.broker.fake import x\n"}, DECISION),
    "risk 直接依赖 broker": ({"server/risk/rules.py": "import server.broker\n"}, ALLOWLIST),
    "storage 直接依赖 broker": ({"server/storage/r.py": "import server.broker\n"}, ALLOWLIST),
    "ipc 直接依赖 broker": ({"server/ipc/outbox.py": "import server.broker.fake\n"}, ALLOWLIST),
    "ai 依赖账本": ({"server/ai/a.py": "import server.storage\n"}, NO_LEDGER),
    "strategy 依赖风控参数": ({"server/strategy/s.py": "from server import risk\n"}, NO_LEDGER),
    "ai 依赖 api": ({"server/ai/a.py": "import server.api\n"}, NO_API),
    "strategy 依赖平台层": ({"server/strategy/s.py": "import server.platforms\n"}, CHANNEL),
    "ai 依赖进程间通道": ({"server/ai/a.py": "from server.ipc import transport\n"}, CHANNEL),
    "平台层依赖 ipc": ({"server/platforms/w.py": "import server.ipc\n"}, LEAF),
    "平台层依赖 storage": ({"server/platforms/w.py": "from server.storage import x\n"}, LEAF),
    "平台层依赖 risk": ({"server/platforms/w.py": "import server.risk\n"}, LEAF),
    "domain 依赖平台层": ({"server/domain/x.py": "import server.platforms\n"}, DOMAIN),
    "平台层依赖 broker": ({"server/platforms/w.py": "import server.broker\n"}, ALLOWLIST),
}


@pytest.mark.parametrize(("files", "contract"), VIOLATIONS.values(), ids=VIOLATIONS.keys())
def test_violation_breaks_contract(tmp_path: Path, files: dict[str, str], contract: str) -> None:
    result = _lint(_project(tmp_path, files))
    assert result.returncode != 0, result.stdout + result.stderr
    broken = [line for line in result.stdout.splitlines() if line.rstrip().endswith("BROKEN")]
    assert any(contract in line for line in broken), result.stdout


ALLOWED = {
    "oms 依赖 broker": {"server/oms/core.py": "import server.broker\n"},
    "guard 依赖 broker": {"server/guard/watch.py": "import server.broker\n"},
    "backtest 依赖 broker.fake": {"server/backtest/run.py": "import server.broker.fake\n"},
    "data 依赖 broker.ibkr": {"server/data/ibkr_hist.py": "import server.broker.ibkr\n"},
    "risk 经 oms 间接依赖 broker": {
        "server/oms/core.py": "import server.broker\n",
        "server/risk/gate.py": "import server.oms.core\n",
    },
    "strategy 依赖 domain": {"server/strategy/s.py": "import server.domain\n"},
    "ipc 依赖平台层": {"server/ipc/t.py": "import server.platforms\n"},
    "data 依赖平台层": {"server/data/l.py": "from server import platforms\n"},
}


@pytest.mark.parametrize("files", ALLOWED.values(), ids=ALLOWED.keys())
def test_allowed_import_is_not_flagged(tmp_path: Path, files: dict[str, str]) -> None:
    result = _lint(_project(tmp_path, files))
    assert result.returncode == 0, result.stdout + result.stderr
