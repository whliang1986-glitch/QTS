"""调用级架构扫描（F-03）：import-linter 只看 import 图，这里看名称的使用、反射与导入白名单。

静态扫描无法穷尽运行时手法，所以策略是“失败即关闭”：凡是能把名称或模块变成运行时数据的
反射原语，在 oms 以外一律只允许最朴素的字面量直接调用，其余形式（取别名、加前缀、变量参数）都报。
运行时兜底由 R-01 / O-01 / B-01 的类型层保护与进程隔离承接。

规则：
- order-exit：下单 / 改单方法名只能在 server/oms 使用（broker 包内的委托实现除外）。
- cancel-exit：撤单方法名只能在 server/oms、server/guard 使用（broker 委托除外）。
- global-cancel：全局撤单在任何包都禁止（会撤掉保护性止损，V4.1 7.6）。
- risk-bypass：oms 的订单提交入口只能在 server/risk（及 oms 自身）使用。
- mode-write：账本的模式变更写入只能在 server/risk/mode_manager.py（及 storage 自身）使用。
- broker-forwarder：broker 包内的受保护名称只能出现在受保护名函数中，且该函数的允许调用方
  必须是被使用名称允许调用方的子集；broker 包内不得定义模块级 __getattr__；
  受保护名函数只能使用白名单装饰器。
- dynamic-name：在无权使用的包里，受保护名称以字符串出现（分派表等）。
- dynamic-call：oms 以外，反射原语（getattr、setattr、delattr、__import__、import_module、
  attrgetter、methodcaller、resolve_name、inspect.getmembers、vars、dir、globals、locals、
  __dict__、__getattribute__、sys.modules、runpy、importlib.util、builtins.*）只能以
  “参数全为字面量的直接调用”出现；登记在 DYNAMIC_EXEMPTIONS 的函数除外。
- broker-import / oms-import / domain-import：静态导入白名单（含相对导入、`server.xxx`
  属性访问、导入函数的字面量参数；导入 server 根包视为导入全部分层）。
- broker-sdk：ib_async / ib_insync 只能在 server/broker/ibkr 中导入。
- domain-io：domain 只能导入白名单内的纯计算模块，不得使用 open / print / exec 等内置 I/O。
- domain-clock：domain 不得读取系统时钟（.now / .utcnow / .today，含字符串形式），时间由调用方传入。
- parse-error：无法解析的文件或相对导入按违规处理。

受保护名称与豁免表集中在下面的常量里；B-01（BrokerAdapter）与 O-01 / R-01（oms 提交入口）
定接口时必须同步更新，并保持 tests/arch/test_order_exit_scan.py 通过。本文件受 CODEOWNERS 保护。

用法：python scripts/arch_scan.py [server 目录]，有违规时退出码为 1。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- 已登记的服务端分层；新增分层必须先在这里登记并补齐依赖规则（tests/arch 会检查） ----
KNOWN_LAYERS = frozenset(
    {
        "domain", "storage", "ipc", "broker", "oms", "risk", "data",
        "strategy", "backtest", "ai", "api", "guard",
    }
)  # fmt: skip

# ---- 受保护名称（B-01 / O-01 / R-01 定稿时同步更新）；含 ib_async 的驼峰原名 ----
CATEGORIES: dict[str, frozenset[str]] = {
    "order": frozenset({"place_order", "submit_order", "modify_order", "placeOrder"}),
    "cancel": frozenset({"cancel_order", "cancelOrder"}),
    "global-cancel": frozenset({"reqGlobalCancel", "global_cancel", "cancel_all_orders"}),
    "submit": frozenset({"submit_approved_intent"}),
    # 模式变更只能经 ModeManager 写入账本（C-04）
    # 以及账户类型基线所用的审计动作名（防止其他模块写同名审计伪造基线）
    "mode-write": frozenset({"append_mode_change", "commit_mode_and_audits", "account_observed"}),
}
NAME_CATEGORY = {name: cat for cat, names in CATEGORIES.items() for name in names}
CATEGORY_RULE = {
    "order": "order-exit",
    "cancel": "cancel-exit",
    "global-cancel": "global-cancel",
    "submit": "risk-bypass",
    "mode-write": "mode-write",
}
CATEGORY_ALLOWED: dict[str, tuple[str, ...]] = {
    "order": ("oms", "broker"),
    "cancel": ("oms", "guard", "broker"),
    "global-cancel": (),
    "submit": ("risk", "oms"),
    "mode-write": ("storage", "risk/mode_manager.py"),
}
CATEGORY_HINT = {
    "order": "只能在 server/oms 中使用",
    "cancel": "只能在 server/oms 或 server/guard 中使用",
    "global-cancel": "任何包都不得使用全局撤单",
    "submit": "只能由 server/risk 调用",
    "mode-write": "只能由 server/risk/mode_manager.py 调用",
}
# broker 包内受保护名函数允许使用的装饰器
BROKER_DECORATOR_ALLOWLIST = frozenset(
    {"abstractmethod", "abc.abstractmethod", "override", "typing.override",
     "staticmethod", "classmethod"}
)  # fmt: skip

# ---- 导入白名单 ----
BROKER_IMPORT_ALLOWED = ("oms", "guard", "backtest", "data", "broker")
OMS_IMPORT_ALLOWED = ("oms", "risk", "backtest")
SDK_ALLOWED = ("broker/ibkr",)
BROKER_SDKS = frozenset({"ib_async", "ib_insync"})

# ---- domain 无 I/O、无时钟（C-01）：导入白名单，新模块默认拒绝 ----
DOMAIN_ALLOWED_MODULES = frozenset(
    {
        "__future__", "typing", "typing_extensions", "decimal", "datetime", "enum",
        "dataclasses", "abc", "collections", "functools", "itertools", "re", "math",
        "types", "pydantic", "annotated_types", "server",
    }
)  # fmt: skip
# 不经 import 就能做 I/O 或执行代码的内置名
DOMAIN_FORBIDDEN_BUILTINS = frozenset(
    {"open", "input", "print", "exec", "eval", "compile", "breakpoint"}
)
DOMAIN_CLOCK_ATTRS = frozenset({"now", "utcnow", "today"})

# ---- 反射原语 ----
DYNAMIC_ALLOWED = ("oms",)
# 登记的豁免：{"server/storage/mapping.py:to_row"}，需 Dylan 审批（CODEOWNERS）
DYNAMIC_EXEMPTIONS: frozenset[str] = frozenset()
# 名称 → 直接调用时必须为字面量的参数下标；None 表示任何形式都禁止
REFLECTION: dict[str, tuple[int, ...] | None] = {
    "getattr": (1,),
    "setattr": (1,),
    "delattr": (1,),
    "hasattr": (1,),
    "getattr_static": (1,),
    "__import__": (0,),
    "import_module": (0,),
    "attrgetter": (0,),
    "methodcaller": (0,),
    "resolve_name": (0,),
    "getmembers": None,
    "vars": None,
    "dir": None,
    "globals": None,
    "locals": None,
}
IMPORT_FUNCS = frozenset({"__import__", "import_module", "resolve_name"})
FORBIDDEN_ATTRS = frozenset(
    {"__dict__", "__getattribute__", "__setattr__", "__delattr__", "modules"}
)
FORBIDDEN_MODULE_PREFIXES = ("runpy", "importlib.util", "builtins")
# 以属性名为参数的反射函数：字面量参数也不得是 dunder 或敏感属性（getattr(x, "__dict__")）
ATTR_NAME_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr", "getattr_static"})
FORBIDDEN_NAMES = frozenset({"__builtins__"})
# getmembers 只在来自 inspect 时算反射（tarfile.getmembers 等无关）
QUALIFIED_ONLY = {"getmembers": "inspect"}


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def _inner(rel: str) -> str:
    """server/oms/core.py → oms/core.py"""
    return rel.split("/", 1)[1] if "/" in rel else ""


def _in(rel: str, allowed: tuple[str, ...]) -> bool:
    inner = _inner(rel)
    return any(inner == a or inner.startswith(a + "/") for a in allowed)


def _top(module: str) -> str:
    return module.split(".", 1)[0]


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_str(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_sensitive_attr(name: object) -> bool:
    return isinstance(name, str) and (
        name in FORBIDDEN_ATTRS or (name.startswith("__") and name.endswith("__"))
    )


def _has_prefix(dotted: str, prefixes: tuple[str, ...]) -> bool:
    """dotted 等于某个前缀，或以“前缀.”开头（builtins_cache 不算 builtins）。"""
    return any(dotted == p or dotted.startswith(p + ".") for p in prefixes)


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, server_aliases: set[str]) -> None:
        self.rel = rel
        self.found: list[Violation] = []
        self.in_broker = _in(rel, ("broker",))
        self.in_domain = _in(rel, ("domain",))
        self.dynamic_ok = _in(rel, DYNAMIC_ALLOWED)
        self.functions: list[str] = []
        self.server_aliases = server_aliases
        self.ok_reflection: set[int] = set()  # 已核准为“字面量直接调用”的 func 节点
        # 所在包的点分路径，用于解析相对导入（__init__.py 的包就是它所在的目录）
        self.package = rel.removesuffix(".py").split("/")[:-1]

    # ---------- 工具 ----------

    def _add(self, node: ast.AST, rule: str, message: str) -> None:
        self.found.append(Violation(self.rel, getattr(node, "lineno", 1), rule, message))

    def _check_name_use(self, node: ast.AST, name: str, *, as_string: bool = False) -> None:
        cat = NAME_CATEGORY.get(name)
        if cat is None:
            return
        if not _in(self.rel, CATEGORY_ALLOWED[cat]):
            rule = "dynamic-name" if as_string else CATEGORY_RULE[cat]
            self._add(node, rule, f"{name} {CATEGORY_HINT[cat]}")
            return
        if self.in_broker:
            enclosing = self.functions[-1] if self.functions else None
            outer = NAME_CATEGORY.get(enclosing or "")
            callers_ok = outer is not None and set(CATEGORY_ALLOWED[outer]) <= set(
                CATEGORY_ALLOWED[cat]
            )
            if not callers_ok:
                self._add(
                    node,
                    "broker-forwarder",
                    f"broker 包内的 {name} 只能出现在调用方不更宽的受保护名函数中"
                    f"（当前：{enclosing}）",
                )

    def _check_import_target(self, node: ast.AST, module: str) -> None:
        if _top(module) in BROKER_SDKS and not _in(self.rel, SDK_ALLOWED):
            self._add(node, "broker-sdk", f"{module} 只能在 server/broker/ibkr 中导入")
        if self.in_domain and _top(module) not in DOMAIN_ALLOWED_MODULES:
            self._add(node, "domain-io", f"domain 不得导入 {module}（领域层只允许纯计算模块）")
        parts = module.split(".")
        if parts[0] != "server":
            return
        # 导入 server 根包等于拿到全部分层
        targets = [parts[1]] if len(parts) > 1 else sorted(KNOWN_LAYERS)
        own = _inner(self.rel).split("/", 1)[0].removesuffix(".py")
        if "broker" in targets and not _in(self.rel, BROKER_IMPORT_ALLOWED):
            self._add(node, "broker-import", f"server/{own} 不得导入 {module}")
        if "oms" in targets and not _in(self.rel, OMS_IMPORT_ALLOWED):
            self._add(node, "oms-import", f"server/{own} 不得导入 {module}（只有 risk、backtest）")
        if own == "domain" and any(t != "domain" for t in targets):
            self._add(node, "domain-import", f"domain 不得依赖 {module}")

    def _resolve_relative(self, module: str | None, level: int) -> str | None:
        if level == 0:
            return module
        base = self.package[: len(self.package) - (level - 1)]
        if not base:
            return None
        return ".".join(base + ([module] if module else []))

    def _dynamic(self, node: ast.AST, what: str) -> None:
        if self.dynamic_ok:
            return
        func = ".".join(self.functions) or "<module>"
        if f"{self.rel}:{func}" in DYNAMIC_EXEMPTIONS:
            return
        self._add(node, "dynamic-call", f"{what}（反射只允许字面量直接调用）")

    def _reflection_name(self, node: ast.expr) -> str | None:
        """node 指向一个反射原语时返回其短名。"""
        dotted = _dotted(node) or ""
        last = dotted.rsplit(".", 1)[-1]
        if last not in REFLECTION:
            return None
        qualifier = QUALIFIED_ONLY.get(last)
        if qualifier is not None and "." in dotted and not dotted.startswith(qualifier + "."):
            return None
        return last

    # ---------- 函数与装饰器 ----------

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.in_broker:
            if node.name == "__getattr__" and not self.functions:
                self._add(node, "broker-forwarder", "broker 包内不得定义模块级 __getattr__")
            if node.name in NAME_CATEGORY:
                for deco in node.decorator_list:
                    target = deco.func if isinstance(deco, ast.Call) else deco
                    if (_dotted(target) or "") not in BROKER_DECORATOR_ALLOWLIST:
                        self._add(deco, "broker-forwarder", f"{node.name} 不得使用非白名单装饰器")
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # ---------- 表达式 ----------

    def visit_Call(self, node: ast.Call) -> None:
        name = self._reflection_name(node.func)
        if name is not None:
            literal_slots = REFLECTION[name]
            plain = isinstance(node.func, ast.Name) or (_dotted(node.func) or "").startswith(
                ("importlib.", "operator.", "inspect.", "pkgutil.")
            )
            ok = (
                literal_slots is not None
                and plain
                and not node.keywords
                and all(i < len(node.args) and _is_str(node.args[i]) for i in literal_slots)
                # 导入函数只允许单个绝对模块名：禁止 package / fromlist 等相对或批量形式
                and (name not in IMPORT_FUNCS or len(node.args) == 1)
                and (name not in {"attrgetter"} or all(_is_str(a) for a in node.args))
                and (
                    name not in ATTR_NAME_FUNCS
                    or not _is_sensitive_attr(getattr(node.args[1], "value", None))
                )
            )
            if ok:
                self.ok_reflection.add(id(node.func))
                arg = node.args[0]
                if name in IMPORT_FUNCS and isinstance(arg, ast.Constant):
                    self._check_import_target(node, str(arg.value).split(":", 1)[0])
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._check_name_use(node, node.id)
        # 只看读取：字段定义 `open: Price` 是赋值目标，不是在用内置 open
        if (
            self.in_domain
            and node.id in DOMAIN_FORBIDDEN_BUILTINS
            and isinstance(node.ctx, ast.Load)
        ):
            self._add(node, "domain-io", f"domain 不得使用内置 {node.id}")
        # 裸名（含 from inspect import getmembers 引入的）只要不是已核准的字面量直接调用就报
        if (
            node.id in REFLECTION and id(node) not in self.ok_reflection
        ) or node.id in FORBIDDEN_NAMES:
            self._dynamic(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_name_use(node, node.attr)
        if self.in_domain and node.attr in DOMAIN_CLOCK_ATTRS:
            self._add(
                node, "domain-clock", f"domain 不得读取系统时钟（.{node.attr}），时间由调用方传入"
            )
        if isinstance(node.value, ast.Name) and node.value.id in self.server_aliases:
            self._check_import_target(node, f"server.{node.attr}")
        dotted = _dotted(node) or ""
        forbidden_attr = node.attr in FORBIDDEN_ATTRS and (
            node.attr != "modules" or dotted == "sys.modules"
        )
        unapproved = id(node) not in self.ok_reflection and self._reflection_name(node) is not None
        if forbidden_attr or _has_prefix(dotted, FORBIDDEN_MODULE_PREFIXES) or unapproved:
            self._dynamic(node, dotted or node.attr)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._check_name_use(node, node.value, as_string=True)
            if self.in_domain and node.value in DOMAIN_CLOCK_ATTRS:
                # 防 getattr(datetime, "now") 这类字面量绕过
                self._add(node, "domain-clock", f"domain 不得以字符串引用时钟方法 {node.value!r}")

    # ---------- 导入 ----------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import_target(node, alias.name)
            if _has_prefix(alias.name, FORBIDDEN_MODULE_PREFIXES):
                self._dynamic(node, alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = self._resolve_relative(node.module, node.level)
        if module is None:
            self._add(node, "parse-error", "无法解析的相对导入")
            return
        self._check_import_target(node, module)
        if _has_prefix(module, FORBIDDEN_MODULE_PREFIXES):
            self._dynamic(node, module)
        for alias in node.names:
            self._check_name_use(node, alias.name)
            if (module == "sys" and alias.name == "modules") or alias.name in FORBIDDEN_NAMES:
                self._dynamic(node, f"from {module} import {alias.name}")
            if alias.name in REFLECTION and alias.asname not in (None, alias.name):
                self._dynamic(node, f"{alias.name} 取别名为 {alias.asname}")
            if alias.name != "*":
                self._check_import_target(node, f"{module}.{alias.name}")


def _collect_server_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "server" and alias.asname:
                    aliases.add(alias.asname)
                elif _top(alias.name) == "server" and alias.asname is None:
                    aliases.add("server")
    return aliases


def scan_file(path: Path, rel: str) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        return [Violation(rel, getattr(exc, "lineno", None) or 1, "parse-error", str(exc))]
    visitor = _Visitor(rel, _collect_server_aliases(tree))
    visitor.visit(tree)
    return visitor.found


def scan(server_dir: Path) -> list[Violation]:
    server_dir = server_dir.resolve()
    base = server_dir.parent
    violations: list[Violation] = []
    for path in sorted(server_dir.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        violations.extend(scan_file(path, rel))
    return sorted(set(violations))


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    server_dir = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1] / "server"
    if not server_dir.is_dir():
        print(f"找不到目录：{server_dir}")
        return 1
    violations = scan(server_dir)
    for v in violations:
        print(v)
    if violations:
        print(f"架构扫描发现 {len(violations)} 处违规。")
        return 1
    print("架构扫描通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
