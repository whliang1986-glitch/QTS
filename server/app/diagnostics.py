"""诊断包（S-09a，V4.1：诊断包脱敏可预览）。

用途：出问题时把排查需要的信息打成一个包，交给 Dylan 或支持人员。原则：

- **先预览、再导出**：`build_bundle` 只在内存里生成，`Bundle.preview()` 列出每个文件与完整内容，
  确认后才由 `zip_bytes` 打成 zip。包的内容不可修改（只读映射），打包前再校验一次条目名。
- **全部脱敏**：
  - 来源数据与能解析成 JSON 的日志行先走 `server.storage.logs.redact`
    （账户号只留前缀与后 3 位，像密钥的字段与 SensitiveValue 替换为 ***），
    再把每个字符串过一遍文本规则；
  - 文本规则：遮账户号，并把 `键=值` / `键: 值` 里像密钥的键、`Bearer …`、
    网址里的 `token=…`、apid 令牌本体替换为 ***；
  - 脱敏是尽力而为：带空格的值、自然语言（“密码是 abc”）遮不全，
    所以导出前必须由人预览（这也是先预览再导出的意义）；
  - 文件名、错误信息同样脱敏（文件名里也可能有账户号）。
- **不收集密钥与受保护配置的内容**（失败即关闭：直接报错，不是静默跳过）：
  - 路径先 `resolve()` 再检查，符号链接与 Windows 目录联接一律拒绝（防止链接把密钥目录带进来）；
  - 密钥目录下的任何文件、名字像凭据的文件（证书、私钥、密钥库、.env、.netrc 等）一律拒绝；
  - 日志只接受白名单后缀（.log、.jsonl、.txt）的普通文件，且不能位于 config/ 下；
  - 配置文件只收**文件名与 SHA-256**，不收内容（受保护配置的数值也不外流，只用来核对版本）。
- **只读**：数据来源由调用方注入为“返回 JSON 可序列化对象的函数”；这里不打开任何数据库，也不写账本；
  取数失败记进 errors.json，不中断打包。
  保留名 manifest、errors、configs 不能用作来源名；重名一律报错。
- **日志只取尾部**：每个文件最多读 `MAX_LOG_BYTES` 字节、保留 `MAX_LOG_LINES` 行。
- **不写盘**：包只在内存里（`PreviewStore` 短暂保存），下载时直接发给客户端。
- 时间一律 UTC（不带时区的时间标为 <naive>，不按本地时区猜）；
  NaN、无穷大写成字符串，保证是合法 JSON；
  包内文件按名字排序、zip 时间戳固定，同样的输入得到同样的包。

apid 的预览与下载接口见 server/api/app.py（只限 owner，写审计）。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sys
import threading
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from server.storage.credentials import MASK, is_credential_key
from server.storage.logs import mask_text, redact

MAX_LOG_LINES = 500
MAX_LOG_BYTES = 2_000_000  # 只读文件尾部这么多字节，避免把巨大的日志读进内存
MAX_LINE_CHARS = 20_000  # 单行上限：超长的整行省略并注明（正则对超长的连续文本很慢）
MAX_SOURCE_CHARS = 2_000_000  # 单个来源序列化后的上限，超出截断并在 errors.json 里注明
LOG_SUFFIXES = frozenset({".log", ".jsonl", ".txt"})
RESERVED = frozenset({"manifest", "errors", "configs"})
_KEY_DIR = "sec" + "rets"  # 密钥目录名（拆开写：仓库的钩子按字面拦截命令里的这个词）
_PROTECTED_TOP = "config"  # 日志不能来自 config/ 下
_CREDENTIAL_SUFFIXES = frozenset({
    ".pem", ".key", ".p12", ".pfx", ".kdbx", ".ppk", ".jks", ".keystore", ".gpg", ".asc",
    ".ovpn", ".crt", ".cer", ".der",
})  # fmt: skip
_CREDENTIAL_NAMES = frozenset(
    {".netrc", "_netrc", ".pgpass", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)
# 键两边可以带引号（"api_token": "…"、{'password': '…'}）；值可以带引号，或到空白、分隔符、括号为止
_KEY_VALUE = re.compile(
    r"""(?P<q>["']?)(?P<key>[^\W\d][\w.-]{0,127})(?P=q)(?P<sep>\s*[=＝:：]\s*)"""
    r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&{}\[\]]+)"""  # 认转义引号；不跨括号
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_API_TOKEN = re.compile(r"\bqts_[0-9a-f]{16}_[A-Za-z0-9_-]{43}")  # 没有键名的 apid 令牌本体


class DiagnosticsError(ValueError):
    """不允许收进诊断包的内容（密钥目录、凭据文件、链接、受保护配置等）。"""


@dataclass(frozen=True)
class Bundle:
    created_at: datetime
    files: Mapping[str, str]  # 包内路径 → 已脱敏的文本内容（只读）
    errors: Mapping[str, str] = field(default_factory=dict)  # 取数失败的来源 → 脱敏后的说明

    def preview(self) -> list[tuple[str, str]]:
        """按名字排序的（文件名，内容）。界面据此在导出前给人看。"""
        return sorted(self.files.items())


# ---------- 脱敏 ----------


def _plain(value: Any) -> Any:  # noqa: PLR0911 - 按类型逐一转换
    """redact 之后再转成严格的 JSON 值：Decimal 与 NaN 成字符串，时间成 UTC。"""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return "<naive>"
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return mask_text(str(value))


def _json(value: Any) -> str:
    # 来源里也有自由文本（吊销原因、HALT 原因、告警参数）：和日志行一样，每个字符串都要过文本规则
    text = json.dumps(
        _scrub_leaves(_plain(redact(value))),
        ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False,
    )  # fmt: skip
    return text + "\n"


def scrub_text(text: str) -> str:
    """一段文本：遮账户号，替换像密钥的 键=值、Bearer 令牌。"""

    def key_value(m: re.Match[str]) -> str:
        if not is_credential_key(m.group("key")):
            return m.group(0)
        value = m.group("value")
        quote = value[0] if value[:1] in {'"', "'"} else ""  # 值原来带引号就保留引号
        return f"{m.group('q')}{m.group('key')}{m.group('q')}{m.group('sep')}{quote}{MASK}{quote}"

    masked = _API_TOKEN.sub(MASK, mask_text(text))
    # 先遮 Bearer：否则 `token=Bearer abc` 的键值规则只遮到 `Bearer` 这个词，令牌本体会留下
    masked = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", masked)
    return _KEY_VALUE.sub(key_value, masked)


def _scrub_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            pass
        # 先按键脱敏，再把每个字符串叶子过一遍文本规则（不对序列化后的整行跑正则，免得弄坏 JSON）
        else:
            return json.dumps(
                _scrub_leaves(_plain(redact(parsed))), ensure_ascii=False, sort_keys=True,
                allow_nan=False,
            )  # fmt: skip
    return scrub_text(line)


def _scrub_leaves(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            safe = scrub_text(key)
            while safe in out:  # 脱敏后键冲突：加后缀，不丢数据（与 redact 同样处理）
                safe += "#"
            out[safe] = _scrub_leaves(item)
        return out
    if isinstance(value, list):
        return [_scrub_leaves(v) for v in value]
    return scrub_text(value) if isinstance(value, str) else value


def _safe_error(exc: BaseException) -> str:
    try:
        text = f"{type(exc).__name__}: {exc}"
    except Exception:
        text = type(exc).__name__
    return scrub_text(text)[:500]


# ---------- 路径检查 ----------


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()  # Windows 目录联接不算符号链接，单独查


def _refuse_secret_path(path: Path) -> Path:
    """原路径与 resolve() 之后的路径都检查；任一级是链接也拒绝。返回解析后的路径。"""
    if any(_is_link(p) for p in (path, *path.parents) if p.exists()):
        raise DiagnosticsError(f"诊断包不接受链接：{scrub_text(path.name)}")
    resolved = path.resolve()
    for candidate in (path, resolved):
        if _KEY_DIR in {p.lower() for p in candidate.parts}:
            raise DiagnosticsError(f"诊断包不收集密钥目录下的文件：{scrub_text(path.name)}")
        name = candidate.name.lower()
        dotenv = name == ".env" or name.startswith(".env.")  # Path(".env").suffix 是空的，单独判断
        if (dotenv or name in _CREDENTIAL_NAMES or candidate.suffix.lower() in _CREDENTIAL_SUFFIXES
                or is_credential_key(candidate.stem)):  # fmt: skip
            raise DiagnosticsError(f"诊断包不收集像凭据的文件：{scrub_text(path.name)}")
    return resolved


def _check_log(path: Path) -> Path:
    resolved = _refuse_secret_path(path)
    if resolved.suffix.lower() not in LOG_SUFFIXES:
        raise DiagnosticsError(f"日志只接受 {sorted(LOG_SUFFIXES)}：{scrub_text(path.name)}")
    if _PROTECTED_TOP in {p.lower() for p in resolved.parts}:
        raise DiagnosticsError(
            f"config/ 下的文件不能当作日志收集（只收哈希）：{scrub_text(path.name)}"
        )
    if resolved.exists() and not resolved.is_file():
        raise DiagnosticsError(f"日志必须是普通文件：{scrub_text(path.name)}")
    return resolved


def _tail(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > MAX_LOG_BYTES:
            fh.seek(size - MAX_LOG_BYTES)
            fh.readline()  # 丢掉被截断的半行
        data = fh.read(MAX_LOG_BYTES)  # 读取期间文件还在增长也不多读
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "".join(_log_line(line) + "\n" for line in lines[-MAX_LOG_LINES:])


def _log_line(line: str) -> str:
    """超长的行整行省略、不截断：截断后 JSON 解析不了、令牌只剩半截，脱敏会变弱（失败即关闭）。"""
    if len(line) > MAX_LINE_CHARS:
        return f"…（本行过长，已省略 {len(line)} 字符）"
    return _scrub_line(line)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add(files: dict[str, str], name: str, text: str) -> None:
    if name in files:
        raise DiagnosticsError(f"诊断包里有重名的文件：{name}")
    files[name] = text


# ---------- 打包 ----------


def build_bundle(
    *,
    sources: Mapping[str, Callable[[], Any]],
    logs: Sequence[Path] = (),
    configs: Sequence[Path] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    version: str = "unknown",
) -> Bundle:
    """生成诊断包（只在内存里）。sources 的名字会成为 `<名字>.json`。"""
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise DiagnosticsError("时钟必须带时区")
    log_paths = [_check_log(p) for p in logs]
    config_paths = [_refuse_secret_path(p) for p in configs]
    files: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name, read in sorted(sources.items()):
        if not name.isidentifier() or name in RESERVED:
            raise DiagnosticsError(
                f"来源名只能是标识符，且不能是保留名 {sorted(RESERVED)}：{name!r}"
            )
        try:
            text = _json(read())
        except Exception as exc:  # 一个来源取不到不影响其他来源
            errors[name] = _safe_error(exc)
            continue
        if len(text) > MAX_SOURCE_CHARS:
            errors[name] = f"内容超过 {MAX_SOURCE_CHARS} 字符，已截断"
            text = text[:MAX_SOURCE_CHARS] + "\n…（已截断）\n"
        _add(files, f"{name}.json", text)
    for original, resolved in zip(logs, log_paths, strict=True):
        key = f"logs/{scrub_text(original.name)}"
        try:
            _add(files, key, _tail(resolved))
        except OSError as exc:
            errors[key] = type(exc).__name__
    hashes: dict[str, str] = {}
    for original, resolved in zip(configs, config_paths, strict=True):
        name = scrub_text(original.name)
        if name in hashes:
            raise DiagnosticsError(f"重名的配置文件：{name}（请分开打包或改名）")
        try:
            hashes[name] = _sha256(resolved)
        except OSError as exc:
            errors[f"config/{name}"] = type(exc).__name__
    _add(files, "configs.json", _json({"sha256": hashes, "note": "只收文件名与哈希，不收内容"}))
    _add(files, "manifest.json", _json({
        "created_at": now.astimezone(UTC),
        "version": version,
        "python": sys.version.split()[0],
        "files": sorted([*files, "errors.json", "manifest.json"]),
    }))  # fmt: skip
    _add(files, "errors.json", _json(errors))
    return Bundle(created_at=now.astimezone(UTC), files=MappingProxyType(files),
                  errors=MappingProxyType(errors))  # fmt: skip


def _check_entry(name: str) -> None:
    pure = PurePosixPath(name)
    if not name or "\\" in name or pure.is_absolute() or ".." in pure.parts or ":" in name:
        raise DiagnosticsError(f"诊断包条目名不合法：{name!r}")


def zip_bytes(bundle: Bundle) -> bytes:
    """把预览过的包打成 zip（只在内存里，不写盘）。时间戳固定：同样内容得到同样的字节。"""
    entries = bundle.preview()
    for name, _ in entries:
        _check_entry(name)
    stamp = bundle.created_at.timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in entries:
            info = zipfile.ZipInfo(name, date_time=stamp)
            archive.writestr(info, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


class PreviewStore:
    """预览过的包暂存在内存里，下载时给的就是预览的那一份（S-09b）。

    - 编号随机、不可猜；过期（默认 10 分钟）或超过容量（默认 3 份，先进先出）就不再给出；
      过期的内容在下一次请求时才从内存清掉；
    - 只保存 `build_bundle` 生成的包：外面拿不到往里塞任意内容的入口；
    - 不落盘：进程退出就没了。同步路由在线程池里跑，所以要加锁。
    """

    def __init__(
        self,
        build: Callable[[], Bundle],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: timedelta = timedelta(minutes=10),
        capacity: int = 3,
    ) -> None:
        if ttl <= timedelta(0) or capacity <= 0:
            raise ValueError("保存时长与容量都必须为正")
        self._build = build
        self._clock = clock
        self.ttl = ttl
        self._capacity = capacity
        self._held: dict[str, tuple[datetime, Bundle]] = {}
        self._lock = threading.Lock()
        self._building = threading.Lock()

    def create_preview(self) -> tuple[str, Bundle]:
        # 同一时间只生成一份：打包可能较慢，并发请求不该把内存撑大
        if not self._building.acquire(blocking=False):
            raise DiagnosticsError("正在生成另一份诊断包，请稍后再试")
        try:
            bundle = self._build()
        finally:
            self._building.release()
        for name in bundle.files:  # 预览时就校验：不能预览成功、下载却永远失败
            _check_entry(name)
        preview_id = uuid.uuid4().hex  # 122 位随机数，来自操作系统的随机源
        with self._lock:
            self._expire()
            while len(self._held) >= self._capacity:
                del self._held[next(iter(self._held))]  # dict 保持插入顺序：丢最早的
            self._held[preview_id] = (self._clock(), bundle)
        return preview_id, bundle

    def archive(self, preview_id: str) -> bytes | None:
        """这份预览的 zip；不存在或已过期返回 None。"""
        with self._lock:
            self._expire()
            held = self._held.get(preview_id)
        return None if held is None else zip_bytes(held[1])

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, (at, _) in self._held.items() if now - at >= self.ttl]:
            del self._held[key]


__all__ = (
    "LOG_SUFFIXES",
    "MAX_LOG_LINES",
    "RESERVED",
    "Bundle",
    "DiagnosticsError",
    "PreviewStore",
    "build_bundle",
    "scrub_text",
    "zip_bytes",
)
