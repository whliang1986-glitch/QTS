"""C-06：结构化日志、correlation_id 贯穿与脱敏。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from server.storage.credentials import SensitiveValue, is_credential_key
from server.storage.ledger import LedgerRepository, open_ledger
from server.storage.logs import (
    configure_logging,
    correlation_scope,
    current_correlation_id,
    log_event,
    mask_text,
    record_audit,
    redact,
)
from server.storage.migrate import upgrade

T0 = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    buf = io.StringIO()
    handler = configure_logging(buf, level=logging.DEBUG)
    try:
        yield buf
    finally:
        logging.getLogger().removeHandler(handler)


def lines(buf: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in buf.getvalue().splitlines()]


# ---------- 脱敏 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("账户 U1234567 已连接", "账户 U****567 已连接"),
        ("模拟账户DU7654321", "模拟账户DU****321"),
        ("DF123456 与 U12345", "DF***456 与 U**345"),
        ("订单 101、价格 500.10、SPY", "订单 101、价格 500.10、SPY"),  # 普通数字不动
        ("XU1234567 与 U1234567X", "XU1234567 与 U1234567X"),  # 不是独立的账户号
    ],
)
def test_mask_text(text: str, expected: str) -> None:
    assert mask_text(text) == expected


def test_redact_nested_structures() -> None:
    value = {
        "account": "DU7654321",
        "tws_password": "hunter2",
        "nested": {"API-Key": "k", "items": [SensitiveValue("s"), "U1234567", 3, Decimal("1.5")]},
        "ok": True,
    }
    out = redact(value)
    assert out == {
        "account": "DU****321",
        "tws_password": "***",
        "nested": {"API-Key": "***", "items": ["***", "U****567", 3, Decimal("1.5")]},
        "ok": True,
    }
    assert "hunter2" not in json.dumps(out, default=str)


@pytest.mark.parametrize(
    ("name", "expected"),
    [("password", True), ("auth_token", True), ("private-key", True), ("tokenizer_name", False),
     ("compass", False), ("credentials", True), ("passive", False)],
)  # fmt: skip
def test_credential_key_detection(name: str, expected: bool) -> None:
    assert is_credential_key(name) is expected


# ---------- 日志 ----------


def test_json_lines_with_fields_and_masking(stream: io.StringIO) -> None:
    log = logging.getLogger("qts.test")
    log_event(log, logging.INFO, "下单", account="U1234567", password="hunter2", qty=Decimal(10))  # noqa: S106 - 脱敏测试用的假值
    log.info("登录 %s 账户 %s", SensitiveValue("hunter2"), "DU7654321")
    first, second = lines(stream)
    assert first["message"] == "下单" and first["level"] == "INFO" and first["logger"] == "qts.test"
    assert first["fields"] == {"account": "U****567", "password": "***", "qty": "10"}
    assert second["message"] == "登录 *** 账户 DU****321"
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None
    assert "hunter2" not in stream.getvalue() and "1234567" not in stream.getvalue()


def test_third_party_logger_and_exceptions_are_masked(stream: io.StringIO) -> None:
    other = logging.getLogger("ib_async.client")
    other.warning("account U1234567 connected")
    try:
        raise RuntimeError("账户 DU7654321 不一致")
    except RuntimeError:
        other.exception("失败")
    out = stream.getvalue()
    assert "1234567" not in out and "7654321" not in out
    assert "RuntimeError: 账户 DU****321 不一致" in lines(stream)[1]["exception"]


def test_mapping_args_are_masked(stream: io.StringIO) -> None:
    logging.getLogger("qts").info("账户 %(acct)s", {"acct": "U1234567"})
    assert lines(stream)[0]["message"] == "账户 U****567"


def test_file_target(tmp_path: Path) -> None:
    path = tmp_path / "qts.log"
    handler = configure_logging(path)
    try:
        logging.getLogger("qts").info("U1234567")
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    assert json.loads(path.read_text(encoding="utf-8"))["message"] == "U****567"


# ---------- correlation_id ----------


def test_correlation_scope_nests_and_resets(stream: io.StringIO) -> None:
    assert current_correlation_id() is None
    with correlation_scope("req-1") as cid:
        assert cid == "req-1"
        with correlation_scope() as inner:
            assert inner == "req-1"  # 内层沿用外层
            logging.getLogger("qts").info("x")
        with correlation_scope("req-2"):
            assert current_correlation_id() == "req-2"
        assert current_correlation_id() == "req-1"
    assert current_correlation_id() is None
    with correlation_scope() as fresh:
        assert fresh and fresh != "req-1"
    assert lines(stream)[0]["correlation_id"] == "req-1"


def test_correlation_is_isolated_between_tasks() -> None:
    async def worker(cid: str) -> str | None:
        with correlation_scope(cid):
            await asyncio.sleep(0)
            return current_correlation_id()

    async def main() -> list[str | None]:
        return list(await asyncio.gather(worker("a"), worker("b")))

    assert asyncio.run(main()) == ["a", "b"]


# ---------- 审计 ----------


@pytest.fixture
def repo(tmp_path: Path) -> LedgerRepository:
    path = tmp_path / "ledger.db"
    upgrade(path)
    return LedgerRepository(open_ledger(path))


def test_audit_carries_correlation_and_is_masked(repo: LedgerRepository) -> None:
    with correlation_scope("order-chain-1"):
        event = record_audit(
            repo,
            action="operator_login",
            actor="dylan",
            at=T0,
            detail={"account": "U1234567", "token": "abc", "reason": "开盘前检查"},
        )
    (stored,) = repo.audits("operator_login")
    assert stored == event and stored.correlation_id == "order-chain-1"
    detail = dict(stored.detail)
    assert json.loads(detail["account"]) == "U****567"
    assert json.loads(detail["token"]) == "***"
    assert json.loads(detail["reason"]) == "开盘前检查"


def test_audit_outside_scope_gets_fresh_correlation(repo: LedgerRepository) -> None:
    a = record_audit(repo, action="x", actor="t", at=T0)
    b = record_audit(repo, action="x", actor="t", at=T0)
    assert a.correlation_id != b.correlation_id and a.event_id != b.event_id
    assert len(repo.audits("x")) == 2


# ---------- 第一轮评审补充 ----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("F1234567", "F****567"), ("I1234567", "I****567"), ("DI1234567", "DI****567"),
     ("u1234567", "u****567"), ("acct_U1234567", "acct_U****567")],
)  # fmt: skip
def test_more_account_formats(text: str, expected: str) -> None:
    assert mask_text(text) == expected


def test_mapping_keys_are_masked(stream: io.StringIO, repo: LedgerRepository) -> None:
    log_event(logging.getLogger("qts"), logging.INFO, "持仓", by_account={"U1234567": 10})
    event = record_audit(repo, action="snapshot", actor="t", at=T0, detail={"U7654321": {"qty": 1}})
    assert "1234567" not in stream.getvalue()
    assert "7654321" not in str(event.detail) and "U****321" in str(event.detail)


def test_audit_detail_encodes_decimal_and_time(repo: LedgerRepository) -> None:
    event = record_audit(
        repo,
        action="fill",
        actor="t",
        at=T0,
        detail={
            "qty": Decimal(10),
            "at": T0,
            "legs": [{"px": Decimal("1.5")}],
            "nan": float("nan"),
        },
    )
    detail = {k: json.loads(v) for k, v in event.detail}
    assert detail == {"qty": "10", "at": T0.isoformat(), "legs": [{"px": "1.5"}], "nan": "nan"}


class Exploding:
    def __str__(self) -> str:
        raise RuntimeError("boom U1234567")


def test_redaction_failure_never_raises_or_leaks(stream: io.StringIO) -> None:
    logging.getLogger("qts").info("值 %s", Exploding())  # 不抛异常
    logging.getLogger("qts").info({"password": "hunter2", "acct": "U1234567"})  # 非字符串 msg
    out = stream.getvalue()
    assert "1234567" not in out and "hunter2" not in out and "boom" not in out
    first = lines(stream)[0]
    assert first["message"] == "值 <redaction failed>"  # 参数出错只替换该参数


def test_propagate_false_logger_with_own_handler_is_masked(stream: io.StringIO) -> None:
    own = io.StringIO()
    logger = logging.getLogger("uvicorn.access.test")
    logger.propagate = False
    handler = logging.StreamHandler(own)
    logger.addHandler(handler)
    try:
        logger.warning("GET /x?account=U1234567")
        try:
            raise ValueError("DU7654321")
        except ValueError:
            logger.exception("失败")
    finally:
        logger.removeHandler(handler)
    assert "1234567" not in own.getvalue() and "7654321" not in own.getvalue()
    assert "U****567" in own.getvalue()


def test_stack_info_is_output_and_masked(stream: io.StringIO) -> None:
    def inner_U1234567() -> None:  # noqa: N802 - 函数名里带账户号，验证调用栈也脱敏
        logging.getLogger("qts").info("x", stack_info=True)

    inner_U1234567()
    entry = lines(stream)[0]
    assert "stack" in entry and "1234567" not in entry["stack"]


def test_log_event_accepts_any_field_names(stream: io.StringIO) -> None:
    log_event(logging.getLogger("qts"), logging.INFO, "风控", level="L2", event="x", logger="y")
    assert lines(stream)[0]["fields"] == {"level": "L2", "event": "x", "logger": "y"}


def test_configure_logging_replaces_previous_handler(stream: io.StringIO) -> None:
    second = io.StringIO()
    handler = configure_logging(second)
    try:
        logging.getLogger("qts").info("once")
    finally:
        logging.getLogger().removeHandler(handler)
    assert len(second.getvalue().splitlines()) == 1 and stream.getvalue() == ""


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129])
def test_correlation_id_is_validated(bad: str) -> None:
    with pytest.raises(ValueError, match="correlation_id"), correlation_scope(bad):
        pass


def test_correlation_does_not_cross_threads() -> None:
    seen: list[str | None] = []
    with correlation_scope("main"):
        worker = threading.Thread(target=lambda: seen.append(current_correlation_id()))
        worker.start()
        worker.join()
    assert seen == [None]  # 新线程不继承：跨线程必须显式传递


def test_same_scope_audits_share_correlation(repo: LedgerRepository) -> None:
    with correlation_scope("chain"):
        a = record_audit(repo, action="x", actor="U1234567", at=T0)
        b = record_audit(repo, action="x", actor="t", at=T0)
    assert a.correlation_id == b.correlation_id == "chain" and a.event_id != b.event_id
    assert a.actor == "U****567"


# ---------- 第二轮评审补充 ----------


def test_redacted_mark_cannot_be_forged_via_extra(stream: io.StringIO) -> None:
    logging.getLogger("qts").info("U1234567", extra={"qts_redacted": True})
    assert "1234567" not in stream.getvalue()


def test_account_split_across_msg_and_args_is_masked(stream: io.StringIO) -> None:
    own = io.StringIO()
    logger = logging.getLogger("qts.split")
    handler = logging.StreamHandler(own)
    logger.addHandler(handler)
    try:
        logger.info("U%s", "1234567")
    finally:
        logger.removeHandler(handler)
    assert "1234567" not in own.getvalue() and "1234567" not in stream.getvalue()


def test_cycles_and_key_collisions() -> None:
    loop: list[Any] = [1]
    loop.append(loop)
    assert redact(loop) == [1, "<cycle>"]
    assert redact({"U1111567": 1, "U2222567": 2}) == {"U****567": 1, "U****567#": 2}


def test_format_error_keeps_masked_template(stream: io.StringIO) -> None:
    logging.getLogger("qts").info("账户 U1234567 数量 %s %s", 1)  # noqa: PLE1206 - 故意少给参数
    message = lines(stream)[0]["message"]
    assert message.startswith("<redaction failed>: TypeError") and "U****567" in message
