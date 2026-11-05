"""F-01 冒烟测试：服务端各层包都能导入，且没有提前引入券商 SDK。"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

LAYERS = [
    "server",
    "server.domain",
    "server.storage",
    "server.ipc",
    "server.broker",
    "server.broker.ibkr",
    "server.broker.fake",
    "server.oms",
    "server.risk",
    "server.data",
    "server.strategy",
    "server.backtest",
    "server.ai",
    "server.api",
    "server.guard",
]


@pytest.mark.parametrize("name", LAYERS)
def test_layer_importable(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__, f"{name} 缺少说明职责的模块文档"


def test_no_broker_sdk_loaded() -> None:
    """导入各层包不会顺带加载券商 SDK（ib_async 只在用到 IBKR 适配器的模块时才加载，B-03b）。

    在独立的进程里检查：同一次测试运行里别的测试（IBKR 适配器的测试）会合法地加载 ib_async。
    """
    code = (
        "import importlib, sys\n"
        f"for name in {LAYERS!r}:\n"
        "    importlib.import_module(name)\n"
        "print('ib_async' in sys.modules or 'ib_insync' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
