"""F-01 冒烟测试：服务端各层包都能导入，且没有提前引入券商 SDK。"""

from __future__ import annotations

import importlib
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
    for name in LAYERS:
        importlib.import_module(name)
    assert "ib_async" not in sys.modules
    assert "ib_insync" not in sys.modules
