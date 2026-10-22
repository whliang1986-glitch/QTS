"""F-03 验收：“唯一下单出口”与“不得绕过风控”的调用级扫描（scripts/arch_scan.py）。

每个违规用例都断言报出的是预期规则，防止某条规则失效时被另一条规则顶替着通过。
别名、取方法对象、改名转发、驼峰 SDK 名、拼接字符串等绕过写法来自 F-03 独立评审。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from scripts.arch_scan import Violation, scan

from tests.arch._layout import make_server_tree

ROOT = Path(__file__).resolve().parents[2]


def _scan(tmp_path: Path, files: dict[str, str]) -> list[Violation]:
    make_server_tree(tmp_path, files)
    return scan(tmp_path / "server")


def test_real_repository_is_clean() -> None:
    assert scan(ROOT / "server") == []


def test_cli_exit_codes(tmp_path: Path) -> None:
    make_server_tree(tmp_path, {"server/api/r.py": "broker.place_order(o)\n"})
    script = str(ROOT / "scripts" / "arch_scan.py")
    bad = subprocess.run(
        [sys.executable, script, str(tmp_path / "server")], capture_output=True, check=False
    )
    assert bad.returncode == 1
    assert "server/api/r.py:1" in bad.stdout.decode("utf-8")
    good = subprocess.run(
        [sys.executable, script, str(ROOT / "server")], capture_output=True, check=False
    )
    assert good.returncode == 0, good.stdout.decode("utf-8")


# (路径, 源码, 预期规则)
VIOLATIONS: dict[str, tuple[str, str, str]] = {
    # ---- 唯一下单出口 ----
    "strategy 下单": ("server/strategy/s.py", "self.broker.place_order(order)\n", "order-exit"),
    "api 改单": ("server/api/r.py", "await broker.modify_order(o, price)\n", "order-exit"),
    "risk 下单": ("server/risk/r.py", "adapter.submit_order(o)\n", "order-exit"),
    "guard 下单": ("server/guard/g.py", "broker.place_order(o)\n", "order-exit"),
    "backtest 下单": ("server/backtest/b.py", "sim.place_order(o)\n", "order-exit"),
    "直接调用函数名": ("server/ai/a.py", "place_order(o)\n", "order-exit"),
    "取方法对象再调用": (
        "server/strategy/s.py",
        "p = self.broker.place_order\np(o)\n",
        "order-exit",
    ),
    "partial 包装": (
        "server/data/d.py",
        "import functools\nfunctools.partial(b.place_order, o)()\n",
        "order-exit",
    ),
    "from-import 别名下单": (
        "server/backtest/b.py",
        "from server.broker.fake import place_order as p\np(o)\n",
        "order-exit",
    ),
    "SDK 驼峰下单": ("server/data/d.py", "ib.placeOrder(contract, order)\n", "order-exit"),
    # ---- 撤单 ----
    "strategy 撤单": ("server/strategy/s.py", "broker.cancel_order(oid)\n", "cancel-exit"),
    "data SDK 驼峰撤单": ("server/data/d.py", "ib.cancelOrder(order)\n", "cancel-exit"),
    "oms 全局撤单": ("server/oms/o.py", "self._ib.reqGlobalCancel()\n", "global-cancel"),
    "guard 全局撤单": ("server/guard/g.py", "broker.global_cancel()\n", "global-cancel"),
    "broker 全局撤单": (
        "server/broker/ibkr/c.py",
        "def x(self):\n    self.ib.reqGlobalCancel()\n",
        "global-cancel",
    ),
    # ---- 不得绕过风控 ----
    "strategy 绕过风控提交": (
        "server/strategy/s.py",
        "oms.submit_approved_intent(i)\n",
        "risk-bypass",
    ),
    "guard from-import 别名提交": (
        "server/guard/g.py",
        "from server.oms import submit_approved_intent as go\ngo(i)\n",
        "risk-bypass",
    ),
    # ---- broker 包内改名转发 ----
    "broker 改名转发下单": (
        "server/broker/helpers.py",
        "def go(b, o):\n    b.place_order(o)\n",
        "broker-forwarder",
    ),
    "broker 撤单函数里下单": (
        "server/broker/fake/f.py",
        "def cancel_order(self, o):\n    self._k.place_order(o)\n",
        "broker-forwarder",
    ),
    "broker 模块级下单": ("server/broker/fake/f.py", "kernel.place_order(o)\n", "broker-forwarder"),
    # ---- 字符串形式 ----
    "getattr 取下单方法": (
        "server/strategy/s.py",
        'getattr(b, "place_order")(o)\n',
        "dynamic-name",
    ),
    "methodcaller": (
        "server/strategy/s.py",
        'import operator\noperator.methodcaller("place_order")(b)\n',
        "dynamic-name",
    ),
    "字符串分派表": (
        "server/api/r.py",
        'ACTIONS = {"go": "submit_approved_intent"}\n',
        "dynamic-name",
    ),
    # ---- 动态反射 ----
    "getattr 拼接名称": (
        "server/strategy/s.py",
        "getattr(b, 'place_' + 'order')(o)\n",
        "dynamic-call",
    ),
    "getattr f-string": ("server/strategy/s.py", "getattr(b, f'{x}_order')(o)\n", "dynamic-call"),
    "getattr 反转字符串": (
        "server/strategy/s.py",
        "getattr(b, 'redro_ecalp'[::-1])\n",
        "dynamic-call",
    ),
    "setattr 动态名": ("server/api/r.py", "setattr(obj, name, v)\n", "dynamic-call"),
    "attrgetter 动态名": (
        "server/ai/a.py",
        "from operator import attrgetter\nattrgetter(n)(b)\n",
        "dynamic-call",
    ),
    "import_module 相对 package": (
        "server/strategy/s.py",
        "import importlib\nimportlib.import_module('.broker', 'server')\n",
        "dynamic-call",
    ),
    "import_module 拼接": (
        "server/ai/a.py",
        "import importlib\nimportlib.import_module('server.' + 'broker')\n",
        "dynamic-call",
    ),
    "from importlib 拼接 SDK": (
        "server/data/d.py",
        "from importlib import import_module\nimport_module('ib_' + 'async')\n",
        "dynamic-call",
    ),
    "__import__ fromlist": (
        "server/ai/a.py",
        "__import__('server', fromlist=['broker'])\n",
        "dynamic-call",
    ),
    "inspect.getmembers": (
        "server/strategy/s.py",
        "import inspect\ninspect.getmembers(b)\n",
        "dynamic-call",
    ),
    "runpy": ("server/api/r.py", "import runpy\nrunpy.run_module('x')\n", "dynamic-call"),
    "importlib.util": (
        "server/ai/a.py",
        "import importlib.util\nimportlib.util.find_spec('x')\n",
        "dynamic-call",
    ),
    # ---- 导入白名单 ----
    "importlib 字面量导入 broker": (
        "server/strategy/s.py",
        "import importlib\nimportlib.import_module('server.broker.ibkr')\n",
        "broker-import",
    ),
    "__import__ 字面量导入 broker": (
        "server/ai/a.py",
        '__import__("server.broker")\n',
        "broker-import",
    ),
    "risk 静态导入 broker": (
        "server/risk/r.py",
        "from server.broker import fake\n",
        "broker-import",
    ),
    "相对导入 broker": ("server/strategy/s.py", "from ..broker import fake\n", "broker-import"),
    "相对导入 broker 子模块": (
        "server/api/v1/r.py",
        "from ...broker.ibkr import x\n",
        "broker-import",
    ),
    "server 属性访问 broker": (
        "server/strategy/s.py",
        "import server.domain\nserver.broker.ibkr.X\n",
        "broker-import",
    ),
    "server 别名属性访问 broker": (
        "server/ai/a.py",
        "import server as s\ns.broker.X\n",
        "broker-import",
    ),
    "storage 导入 oms": ("server/storage/r.py", "from server.oms import core\n", "oms-import"),
    "guard 导入 oms": ("server/guard/g.py", "import server.oms\n", "oms-import"),
    "data 相对导入 oms": ("server/data/d.py", "from ..oms import core\n", "oms-import"),
    "domain 导入 storage": (
        "server/domain/x.py",
        "from server.storage import repo\n",
        "domain-import",
    ),
    "domain 相对导入 risk": ("server/domain/x.py", "from ..risk import rules\n", "domain-import"),
    # ---- 券商 SDK 隔离 ----
    "oms 导入 ib_async": ("server/oms/o.py", "import ib_async\n", "broker-sdk"),
    "data 导入 ib_insync": ("server/data/d.py", "from ib_insync import IB\n", "broker-sdk"),
    "broker 根包导入 ib_async": (
        "server/broker/base.py",
        "from ib_async.order import Order\n",
        "broker-sdk",
    ),
    "broker 兄弟模块导入 ib_async": (
        "server/broker/ibkr_shim.py",
        "import ib_async\n",
        "broker-sdk",
    ),
    "字面量动态导入 ib_async": (
        "server/data/d.py",
        "import importlib\nimportlib.import_module('ib_async')\n",
        "broker-sdk",
    ),
    # ---- 第二轮评审：guard / broker 不再豁免反射，反射取别名与变体 ----
    "N1 guard 变量名 getattr": (
        "server/guard/g.py",
        "getattr(self.broker, self.action)(o)\n",
        "dynamic-call",
    ),
    "N2 broker 动态转发器": (
        "server/broker/fake/f.py",
        "def go(self, n, o):\n    return getattr(self._k, n)(o)\n",
        "dynamic-call",
    ),
    "N21 broker 模块级 __getattr__": (
        "server/broker/fake/f.py",
        "def __getattr__(name):\n    return 1\n",
        "broker-forwarder",
    ),
    "N3 builtins.getattr": (
        "server/strategy/s.py",
        "import builtins\nbuiltins.getattr(b, 'x')\n",
        "dynamic-call",
    ),
    "N4 getattr 取别名": ("server/strategy/s.py", "g = getattr\ng(b, n)\n", "dynamic-call"),
    "N5 import_module 取别名": (
        "server/ai/a.py",
        "from importlib import import_module as im\nim(n)\n",
        "dynamic-call",
    ),
    "N16 importlib.__import__": (
        "server/ai/a.py",
        "import importlib\nimportlib.__import__(n)\n",
        "dynamic-call",
    ),
    "N6 __getattribute__": (
        "server/strategy/s.py",
        "object.__getattribute__(b, n)\n",
        "dynamic-call",
    ),
    "N7 vars": ("server/strategy/s.py", "vars(type(b))[n]\n", "dynamic-call"),
    "N8 __dict__": ("server/strategy/s.py", "type(b).__dict__[n]\n", "dynamic-call"),
    "N9 dir": ("server/strategy/s.py", "names = [m for m in dir(b)]\n", "dynamic-call"),
    "N10 sys.modules": ("server/api/r.py", "import sys\nsys.modules[n]\n", "dynamic-call"),
    "N12 __import__ 根包": (
        "server/strategy/s.py",
        "__import__('server').broker\n",
        "broker-import",
    ),
    "N13 import_module 根包": (
        "server/ai/a.py",
        "import importlib\nimportlib.import_module('server').broker\n",
        "broker-import",
    ),
    "N14 sys.modules 根包": (
        "server/ai/a.py",
        "import sys\nsys.modules['server'].broker\n",
        "dynamic-call",
    ),
    "N15 broker 装饰器登记": (
        "server/broker/fake/f.py",
        "@register('go')\ndef place_order(self, o):\n    self._k.place_order(o)\n",
        "broker-forwarder",
    ),
    "from inspect import getmembers": (
        "server/strategy/s.py",
        "from inspect import getmembers\ngetmembers(b)\n",
        "dynamic-call",
    ),
    "pkgutil.resolve_name 导入 broker": (
        "server/strategy/s.py",
        "import pkgutil\npkgutil.resolve_name('server.broker.fake:F')\n",
        "broker-import",
    ),
    "B-01 须内联：私有辅助函数下单": (
        "server/broker/ibkr/a.py",
        "def _send(self, o):\n    self._ib.placeOrder(o)\n",
        "broker-forwarder",
    ),
    # ---- 第三轮评审 ----
    "X4 guard getattr_static": (
        "server/guard/g.py",
        "import inspect\ninspect.getattr_static(b, n)(o)\n",
        "dynamic-call",
    ),
    "X5 getattr_static 取别名": (
        "server/guard/g.py",
        "from inspect import getattr_static as g\ng(b, n)\n",
        "dynamic-call",
    ),
    "X10 guard getattr 取 __dict__": (
        "server/guard/g.py",
        "getattr(type(b), '__dict__')[n](b, o)\n",
        "dynamic-call",
    ),
    "X2 getattr 取 sys.modules": (
        "server/api/r.py",
        "import sys\ngetattr(sys, 'modules')[n]\n",
        "dynamic-call",
    ),
    "X1 from sys import modules": (
        "server/api/r.py",
        "from sys import modules\nmodules[n]\n",
        "dynamic-call",
    ),
    "X3 __builtins__": ("server/strategy/s.py", "__builtins__['getattr'](b, n)\n", "dynamic-call"),
    # ---- C-01：domain 无 I/O、无时钟 ----
    "domain 导入 sqlite3": ("server/domain/x.py", "import sqlite3\n", "domain-io"),
    "domain 导入 os.path": ("server/domain/x.py", "import os.path\n", "domain-io"),
    "domain from pathlib": ("server/domain/x.py", "from pathlib import Path\n", "domain-io"),
    "domain 导入 time": ("server/domain/x.py", "import time\n", "domain-io"),
    "domain 导入 logging": ("server/domain/x.py", "import logging\n", "domain-io"),
    "domain 读系统时钟 now": (
        "server/domain/x.py",
        "from datetime import datetime\nx = datetime.now(UTC)\n",
        "domain-clock",
    ),
    "domain 读系统时钟 today": ("server/domain/x.py", "x = date.today()\n", "domain-clock"),
    "domain 字面量 getattr 取 now": (
        "server/domain/x.py",
        "x = getattr(datetime, 'now')()\n",
        "domain-clock",
    ),
    "domain 导入白名单外模块 pickle": ("server/domain/x.py", "import pickle\n", "domain-io"),
    "domain 导入白名单外模块 zoneinfo": ("server/domain/x.py", "import zoneinfo\n", "domain-io"),
    "domain 使用内置 open": ("server/domain/x.py", "open('x').read()\n", "domain-io"),
    "domain 使用内置 print": ("server/domain/x.py", "print(1)\n", "domain-io"),
    "object.__setattr__ 改冻结对象": (
        "server/strategy/s.py",
        "object.__setattr__(oi, 'side', 'SELL')\n",
        "dynamic-call",
    ),
    # ---- C-04：模式变更只能经 ModeManager ----
    "oms 直接写模式": ("server/oms/o.py", "repo.append_mode_change(c, m, h, t, a)\n", "mode-write"),
    "risk 其他模块写模式": (
        "server/risk/rules.py",
        "repo.commit_mode_and_audits([], change)\n",
        "mode-write",
    ),
    "其他模块伪造账户观察审计": (
        "server/oms/o.py",
        "repo.append_audit(AuditEvent(action='account_observed'))\n",
        "dynamic-name",
    ),
    "api 直接写模式": ("server/api/r.py", "repo.commit_mode_and_audits(a)\n", "mode-write"),
    # ---- 失败即关闭 ----
    "语法错误": ("server/strategy/s.py", "def broken(:\n", "parse-error"),
}


@pytest.mark.parametrize(("path", "source", "rule"), VIOLATIONS.values(), ids=VIOLATIONS.keys())
def test_violation_is_reported(tmp_path: Path, path: str, source: str, rule: str) -> None:
    violations = _scan(tmp_path, {path: source})
    rules = {v.rule for v in violations}
    assert rule in rules, f"{path} 应报 {rule}，实际：{violations}"
    assert all(v.path == path for v in violations)


ALLOWED: dict[str, tuple[str, str]] = {
    "oms 下单": ("server/oms/o.py", "await self.broker.place_order(order)\n"),
    "oms 撤单与改单": ("server/oms/o.py", "b.cancel_order(i)\nb.modify_order(i, p)\n"),
    "oms 动态反射": ("server/oms/o.py", "getattr(self, name)\n"),
    "guard 撤单": ("server/guard/g.py", "broker.cancel_order(order_id)\n"),
    "broker 同名委托": (
        "server/broker/fake/f.py",
        "def place_order(self, o):\n    self._k.place_order(o)\n",
    ),
    "ibkr 驼峰委托": (
        "server/broker/ibkr/a.py",
        "from ib_async import IB\nasync def place_order(self, o):\n    self._ib.placeOrder(o)\n"
        "def cancel_order(self, o):\n    self._ib.cancelOrder(o)\n",
    ),
    "broker 定义接口": (
        "server/broker/base.py",
        "class BrokerAdapter:\n    async def place_order(self, o): ...\n",
    ),
    "risk 调用 oms 提交入口": (
        "server/risk/r.py",
        "from server.oms import core\ncore.submit_approved_intent(i)\n",
    ),
    "risk 相对导入 oms": ("server/risk/r.py", "from ..oms import core\n"),
    "oms 内部调用提交入口": ("server/oms/o.py", "self.submit_approved_intent(i)\n"),
    "backtest 导入 oms 与 broker.fake": (
        "server/backtest/b.py",
        "import server.oms\nfrom server.broker import fake\n",
    ),
    "data 导入 broker.ibkr": ("server/data/d.py", "from server.broker.ibkr import hist\n"),
    "oms 动态导入 broker": ("server/oms/o.py", 'importlib.import_module("server.broker.fake")\n'),
    "strategy 导入 domain": (
        "server/strategy/s.py",
        "from server.domain import order\nfrom ..domain import x\n",
    ),
    "strategy 字面量 getattr": ("server/strategy/s.py", 'getattr(obj, "name", None)\n'),
    "strategy 字面量 import_module": (
        "server/strategy/s.py",
        "import importlib\nimportlib.import_module('json')\n",
    ),
    "domain 内部相对导入": (
        "server/domain/x.py",
        "from . import order\nfrom .order import Order\n",
    ),
    "无关的同名前缀": ("server/strategy/s.py", "self.place_order_count = 0\nlog('placed')\n"),
    # 第二轮评审的误报修正
    "F5 下单失败回滚撤单": (
        "server/broker/ibkr/a.py",
        "def place_order(self, o):\n    try:\n        self._ib.placeOrder(o)\n"
        "    except E:\n        self._ib.cancelOrder(o)\n",
    ),
    "F3 日志名含 server.oms": ("server/guard/g.py", "logging.getLogger('server.oms.watch')\n"),
    "F6 tarfile.getmembers": ("server/data/d.py", "archive.getmembers()\n"),
    "F9 字符串注解": ("server/data/d.py", "def f(h: 'server.broker.ibkr.Hist') -> None: ...\n"),
    "F10 abstractmethod 接口": (
        "server/broker/base.py",
        "import abc\nclass A(abc.ABC):\n    @abc.abstractmethod\n    def place_order(self, o): ...\n",
    ),
    "字面量 hasattr": ("server/api/r.py", "hasattr(model, 'id')\n"),
    "domain 字段名与内置同名": (
        "server/domain/x.py",
        "class Bar:\n    open: int\n    x = self.open\n",
    ),
    "domain 用纯计算标准库与 pydantic": (
        "server/domain/x.py",
        "from datetime import UTC, datetime\nfrom decimal import Decimal\nfrom enum import StrEnum\n"
        "from pydantic import BaseModel\n",
    ),
    "strategy 读系统时钟不受 domain 规则约束": ("server/strategy/s.py", "x = datetime.now(UTC)\n"),
    "ModeManager 写模式": (
        "server/risk/mode_manager.py",
        "self._repo.commit_mode_and_audits(a, c)\n",
    ),
    "F16 builtins 前缀的普通名字": ("server/data/d.py", "builtins_cache.get(k)\nrunpy_like = 1\n"),
}


@pytest.mark.parametrize(("path", "source"), ALLOWED.values(), ids=ALLOWED.keys())
def test_allowed_usage_is_not_reported(tmp_path: Path, path: str, source: str) -> None:
    assert _scan(tmp_path, {path: source}) == []
