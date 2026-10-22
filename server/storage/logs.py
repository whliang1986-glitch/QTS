"""结构化日志与审计事件（C-06）。

- 日志一律输出单行 JSON：UTC 时间、级别、logger、消息、correlation_id、结构化字段、异常、调用栈。
- correlation_id 用 contextvars 贯穿一次请求或一条订单链：`correlation_scope()` 进入时生成或沿用，
  日志与审计事件自动带上。新线程（threading.Thread、run_in_executor）与 ib_async 的协议回调
  不继承调用方的上下文：跨线程、跨进程（ADR-14 outbox）或在券商回调里必须显式传递 correlation_id。
- 脱敏在**创建日志记录时**完成（`configure_logging` 让所有 logger 在 makeRecord 时就地脱敏），
  因此任何 handler——包括第三方库自带的 handler、propagate=False 的 logger（如 uvicorn）——
  拿到的都已是脱敏后的记录；handler 上的过滤器与 JsonFormatter 再各做一次（纵深防御）。
  脱敏本身出错时降级为丢弃内容，绝不向业务代码抛异常。
- 脱敏规则：`SensitiveValue` 一律显示为 ***；字段名像密钥的值整体替换；映射的键同样按文本规则脱敏；
  文本中的 IBKR 账户号（U / F / I / DU / DF / DI + 5–9 位数字，不分大小写）只保留字母前缀与后 3 位。
  异常文本同样按文本规则脱敏，但无法识别任意形态的明文密钥——
  密钥只应以 SensitiveValue 存在，不得拼进异常或消息（CLAUDE.md 红线 7）。
- `record_audit` 把审计事件写入账本（只追加），detail 先脱敏、按 JSON 编码（Decimal 编码为字符串）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from server.domain.modes import AuditEvent
from server.storage.credentials import MASK, SensitiveValue, is_credential_key
from server.storage.ledger import LedgerRepository

# IBKR 账户号：真实账户 U…，FA / IBroker 主账户 F… / I…，模拟账户 DU… / DF… / DI…；宁可误报
ACCOUNT_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:DU|DF|DI|U|F|I)\d{5,9}(?![A-Za-z0-9])", re.I)
_CORRELATION_ID = re.compile(r"\S{1,128}")
_MAX_DEPTH = 20
_REDACTION_FAILED = "<redaction failed>"
_INSTALLED: list[logging.Handler] = []  # configure_logging 装过的 handler

_CORRELATION: ContextVar[str | None] = ContextVar("correlation_id", default=None)
# 已脱敏标记：存私有哨兵对象，extra 无法伪造（extra 只能放可比较的普通值，且不会是这个对象）
_DONE = object()


def mask_account(account_id: str) -> str:
    """保留字母前缀与后 3 位：U1234567 → U****567，DU7654321 → DU****321。"""
    head = re.match(r"[A-Za-z]*", account_id).group(0)  # type: ignore[union-attr]
    digits = account_id[len(head) :]
    if len(digits) <= 3:
        return head + "*" * len(digits)
    return f"{head}{'*' * (len(digits) - 3)}{digits[-3:]}"


def mask_text(text: str) -> str:
    return ACCOUNT_PATTERN.sub(lambda m: mask_account(m.group(0)), text)


def _safe_str(value: object) -> str:
    try:
        return mask_text(str(value))
    except Exception:  # 对象的 __str__ 出错时不暴露任何内容
        return _REDACTION_FAILED


def redact(value: Any, key: str | None = None) -> Any:
    """返回可以安全写入日志或审计的副本（Decimal、数字、布尔、时间原样保留，编码时处理）。"""
    return _redact(value, key, 0, set())


def _redact(value: Any, key: str | None, depth: int, seen: set[int]) -> Any:  # noqa: PLR0911
    if depth > _MAX_DEPTH:
        return "<too deep>"
    if isinstance(value, SensitiveValue) or (key is not None and is_credential_key(key)):
        return MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping | list | tuple | set | frozenset):
        if id(value) in seen:
            return "<cycle>"
        seen = seen | {id(value)}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            safe_key = _safe_str(k)
            while safe_key in out:  # 脱敏后键冲突（如两个后 3 位相同的账户号）：加后缀，不丢数据
                safe_key += "#"
            out[safe_key] = _redact(v, _safe_str(k), depth + 1, seen)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [_redact(v, None, depth + 1, seen) for v in value]
    if isinstance(value, bool | int | float | Decimal | datetime | date | None):
        return value
    return _safe_str(value)


def _jsonable(value: Any) -> Any:  # noqa: PLR0911 - 按类型逐一转换
    """转成严格 JSON 可编码的值：Decimal 为字符串，时间为 ISO 8601，非有限浮点为字符串。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return _safe_str(value)


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, allow_nan=False)


# ---------- correlation_id ----------


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current_correlation_id() -> str | None:
    return _CORRELATION.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """进入一个关联范围：给定 id 则沿用，否则沿用外层，外层也没有就新建。

    id 必须非空、不含空白、不超过 128 字符（与审计事件的 Identifier 一致）。
    """
    if correlation_id is not None and not _CORRELATION_ID.fullmatch(correlation_id):
        raise ValueError("correlation_id 必须非空、不含空白、不超过 128 字符")
    cid = correlation_id or _CORRELATION.get() or new_correlation_id()
    token = _CORRELATION.set(cid)
    try:
        yield cid
    finally:
        _CORRELATION.reset(token)


# ---------- 脱敏记录 ----------


def _redact_record(record: logging.LogRecord) -> None:
    """就地脱敏一条记录（只做一次）；任何异常都降级为丢弃内容，不向调用方抛出。"""
    if getattr(record, "qts_redacted", None) is _DONE:
        return
    template = record.msg
    try:
        record.msg = (
            mask_text(record.msg) if isinstance(record.msg, str) else _safe_str(redact(record.msg))
        )
        template = record.msg
        if isinstance(record.args, Mapping):
            record.args = redact(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        fields = getattr(record, "fields", None)
        record.fields = redact(fields) if isinstance(fields, Mapping) else {}
        cid = getattr(record, "correlation_id", None)
        record.correlation_id = _safe_str(cid) if cid is not None else current_correlation_id()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = mask_text(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_text(record.stack_info)
        # 冻结为拼好并脱敏的文本：msg 与参数拼接出的账户号（"U%s" % "1234567"）也被遮挡
        record.msg = mask_text(record.getMessage())
        record.args = ()
    except Exception as exc:  # 保密优先：宁可丢掉这条日志的内容
        safe_template = template if isinstance(template, str) else _REDACTION_FAILED
        record.msg = f"{_REDACTION_FAILED}: {type(exc).__name__}: {mask_text(safe_template)}"
        record.args = ()
        record.fields = {}
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
    record.qts_redacted = _DONE


class RedactingFilter(logging.Filter):
    """脱敏并补上 correlation_id。挂在 handler 上；不会抛异常。"""

    def filter(self, record: logging.LogRecord) -> bool:
        _redact_record(record)
        return True


class RedactingLogger(logging.Logger):
    """在 makeRecord（合并 extra 之后）就地脱敏：任何 handler 拿到的都是脱敏后的记录。"""

    def makeRecord(self, *args: Any, **kwargs: Any) -> logging.LogRecord:  # noqa: N802 - 覆盖标准库方法
        record = super().makeRecord(*args, **kwargs)
        _redact_record(record)
        return record


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        _redact_record(record)  # 单独使用本 formatter 时也保证脱敏
        try:
            message = record.getMessage()
        except Exception as exc:
            message = f"{_REDACTION_FAILED}: {type(exc).__name__}"
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_text(message),
            "correlation_id": getattr(record, "correlation_id", None),
            "fields": redact(getattr(record, "fields", {})),
        }
        if record.exc_text:
            entry["exception"] = mask_text(record.exc_text)
        if record.stack_info:
            entry["stack"] = mask_text(record.stack_info)
        return _dumps(entry)


def log_event(logger: logging.Logger, level: int, event: str, /, **fields: Any) -> None:
    """记录一条带结构化字段的日志（字段同样脱敏）。字段名可以是 level、event 等任意名字。"""
    logger.log(level, event, extra={"fields": fields})


def _install_redacting_loggers() -> None:
    """让此后创建的与已存在的 logger 都在创建记录时脱敏（不改它们的 handler 与级别）。"""
    logging.setLoggerClass(RedactingLogger)
    # 只应在单线程的启动阶段调用；先拷贝再遍历，避免并发创建 logger 时字典变化
    loggers = [logging.getLogger(), *list(logging.Logger.manager.loggerDict.values())]
    for logger in loggers:
        if type(logger) in {logging.Logger, logging.RootLogger}:
            logger.__class__ = (
                _RedactingRoot if isinstance(logger, logging.RootLogger) else RedactingLogger
            )


class _RedactingRoot(logging.RootLogger, RedactingLogger):
    pass


def configure_logging(
    target: Path | TextIO | None = None, *, level: int = logging.INFO
) -> logging.Handler:
    """给根 logger 装上 JSON + 脱敏 handler（重复调用会替换上一次装的），返回它以便关闭时移除。

    同时让所有 logger 在创建记录时脱敏：第三方库自带的 handler 与 propagate=False 的 logger
    （例如 uvicorn 的 access 日志）也只会拿到脱敏后的记录。
    apid 接入 uvicorn 时仍应接管其 log_config，使输出格式统一为 JSON。
    """
    _install_redacting_loggers()
    root = logging.getLogger()
    while _INSTALLED:
        old = _INSTALLED.pop()
        root.removeHandler(old)
        old.close()
    if isinstance(target, Path):
        handler: logging.Handler = logging.FileHandler(target, encoding="utf-8")
    else:
        handler = logging.StreamHandler(target or sys.stderr)
    _INSTALLED.append(handler)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    return handler


# ---------- 审计 ----------


def record_audit(  # noqa: PLR0913 - 审计事件的字段全部显式给出
    repo: LedgerRepository,
    *,
    action: str,
    actor: str,
    at: datetime,
    detail: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    """写一条审计事件（只追加）。correlation_id 缺省取当前范围，范围外新建一个。

    每次调用生成新的 event_id，不是重试幂等入口；需要幂等的调用方用 LedgerRepository.append_audit
    自带 event_id。actor 与 action 同样按文本规则脱敏（不要把账户号当操作者）。
    """
    cid = correlation_id or current_correlation_id() or new_correlation_id()
    safe = redact(dict(detail or {}))
    event = AuditEvent(
        event_id=f"audit-{uuid.uuid4().hex}",
        occurred_at=at,
        actor=mask_text(actor),
        action=mask_text(action),
        correlation_id=cid,
        detail=tuple(sorted((k, _dumps(v)) for k, v in safe.items())),
    )
    repo.append_audit(event)
    return event
