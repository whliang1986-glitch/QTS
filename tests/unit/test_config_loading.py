"""C-05：TOML 配置加载、密钥字段拦截、ModeConfig 与日历覆盖解析、凭据包装。"""

from __future__ import annotations

import codecs
import logging
import pickle
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import keyring
import pytest
from keyring.errors import NoKeyringError
from server.data.calendar_config import nyse_from_config, parse_calendar_overrides
from server.domain.enums import AccountType, Mode
from server.risk.config import parse_mode_config
from server.storage.config import ConfigError, LoadedConfig, load_toml
from server.storage.credentials import (
    MASK,
    CredentialUnavailableError,
    KeyringCredentialStore,
    MemoryCredentialStore,
    SensitiveValue,
    is_credential_key,
)

NL = chr(10)


def write(tmp_path: Path, text: str, name: str = "c.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


MODE = NL.join(
    [
        "[mode]",
        'requested_mode = "PAPER"',
        'account_id = "DU0000001"',
        'expected_account_type = "CASH"',
        'risk_regime = "CASH"',
        "allow_delayed_data = true",
        "",
    ]
)


# ---------- 加载 ----------


def test_load_is_versioned_by_content_hash(tmp_path: Path) -> None:
    a = load_toml(write(tmp_path, MODE), name="mode")
    b = load_toml(write(tmp_path, MODE, "copy.toml"), name="mode")
    assert a.sha256 == b.sha256 and a.version == f"mode@{a.sha256[:12]}"
    changed = load_toml(write(tmp_path, MODE.replace("true", "false"), "x.toml"), name="mode")
    assert changed.sha256 != a.sha256


def test_content_is_deeply_frozen(tmp_path: Path) -> None:
    loaded = load_toml(write(tmp_path, "[a]" + NL + "xs = [1, 2]" + NL), name="t")
    with pytest.raises(TypeError):
        loaded.content["a"]["xs"] = (3,)
    assert loaded.content["a"]["xs"] == (1, 2)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[a" + NL, "不是合法的 TOML"),
        ("[broker]" + NL + 'password = "x"' + NL, "broker.password"),
        ("[broker]" + NL + 'api_key = "x"' + NL, "api_key"),
        ("[x]" + NL + "[[x.items]]" + NL + 'auth_token = "x"' + NL, r"x\.items\[0\]\.auth_token"),
        ("[db]" + NL + 'Private-Key = "x"' + NL, "Private-Key"),
    ],
)
def test_load_fails_closed(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_toml(write(tmp_path, text), name="t")


def test_credential_error_does_not_echo_the_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as err:
        load_toml(write(tmp_path, "[b]" + NL + 'password = "hunter2"' + NL), name="t")
    assert "hunter2" not in str(err.value)


def test_harmless_names_are_not_mistaken_for_credentials(tmp_path: Path) -> None:
    text = "[x]" + NL + "compass = 1" + NL + 'tokenizer_name = "a"' + NL + "passive = true" + NL
    assert load_toml(write(tmp_path, text), name="t").content["x"]["compass"] == 1


def test_missing_and_undecodable_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        load_toml(tmp_path / "nope.toml", name="t")
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="无法读取"):
        load_toml(bad, name="t")


@pytest.mark.parametrize("name", ["", "Mode", "a b", "../x", "x" * 65])
def test_config_name_is_validated(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="配置名"):
        load_toml(write(tmp_path, MODE), name=name)


# ---------- ModeConfig ----------


def test_mode_config_takes_version_from_loader(tmp_path: Path) -> None:
    loaded = load_toml(write(tmp_path, MODE), name="mode")
    config = parse_mode_config(loaded)
    assert config.requested_mode is Mode.PAPER
    assert config.expected_account_type is AccountType.CASH
    assert config.allow_delayed_data is True
    assert config.config_version == loaded.version


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[other]" + NL + "x = 1" + NL, r"只能有 \[mode\]"),
        (MODE + 'config_version = "v9"' + NL, "config_version"),
        (MODE + "extra = 1" + NL, "extra"),
        (MODE.replace('"PAPER"', '"BACKTEST"'), "mode"),  # 延迟行情只允许 PAPER
        (MODE.replace("allow_delayed_data = true", 'allow_delayed_data = "yes"'), "allow_delayed"),
    ],
)
def test_mode_config_rejects_bad_tables(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_mode_config(load_toml(write(tmp_path, text), name="mode"))


def test_mode_config_error_does_not_echo_account(tmp_path: Path) -> None:
    text = MODE.replace('"DU0000001"', '"DU 12345"')  # 不合法的账户号
    with pytest.raises(ConfigError) as err:
        parse_mode_config(load_toml(write(tmp_path, text), name="mode"))
    assert "12345" not in str(err.value)


# ---------- 日历覆盖 ----------

CAL = NL.join(
    [
        "[calendar]",
        "adhoc_closures = [2026-10-30]",
        "",
        "[[calendar.adhoc_early_closes]]",
        "date = 2026-12-31",
        "close = 13:00:00",
        "",
    ]
)


def test_calendar_overrides_are_parsed_in_new_york_time(tmp_path: Path) -> None:
    closures, early = parse_calendar_overrides(load_toml(write(tmp_path, CAL), name="calendar"))
    assert closures == frozenset({date(2026, 10, 30)})
    assert early == {date(2026, 12, 31): datetime(2026, 12, 31, 18, 0, tzinfo=UTC)}  # EST = UTC−5


def test_calendar_from_config(tmp_path: Path) -> None:
    cal = nyse_from_config(
        load_toml(write(tmp_path, CAL), name="calendar"),
        start=date(2026, 1, 1),
        end=date(2027, 12, 31),
    )
    assert not cal.is_trading_day(date(2026, 10, 30))
    session = cal.session(date(2026, 12, 31))
    assert session is not None and session.early_close
    assert session.close == datetime(2026, 12, 31, 18, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[x]" + NL, r"只能有 \[calendar\]"),
        ("[calendar]" + NL + "holidays = []" + NL, "未知字段"),
        ("[calendar]" + NL + "adhoc_closures = [2026-10-30T00:00:00Z]" + NL, "本地日期"),
        ("[calendar]" + NL + 'adhoc_closures = ["2026-10-30"]' + NL, "本地日期"),
        ("[calendar]" + NL + "adhoc_closures = [2026-10-30, 2026-10-30]" + NL, "重复"),
        ("[calendar]" + NL + "adhoc_closures = 2026-10-30" + NL, "数组"),
        (CAL.replace("close = 13:00:00", "close = 13:00:00" + NL + "note = 1"), "两个字段"),
        (CAL.replace("13:00:00", "2026-12-31T13:00:00"), "本地时间"),
        (CAL + "[[calendar.adhoc_early_closes]]" + NL + "date = 2026-12-31" + NL
         + "close = 12:00:00" + NL, "重复"),
    ],
)  # fmt: skip
def test_calendar_overrides_fail_closed(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_calendar_overrides(load_toml(write(tmp_path, text), name="calendar"))


# ---------- 凭据 ----------


def test_sensitive_value_never_renders(caplog: pytest.LogCaptureFixture) -> None:
    value = SensitiveValue("hunter2")
    rendered = [
        str(value),
        repr(value),
        f"{value}",
        f"{value!r}",
        format(value, ">20"),
    ]
    assert all("hunter2" not in r for r in rendered) and MASK in str(value)
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("登录 %s", value)
        logging.getLogger("t").info("登录 %r", {"pw": value})
    assert "hunter2" not in caplog.text
    assert value.reveal() == "hunter2"


def test_sensitive_value_cannot_be_pickled_or_hashed() -> None:
    value = SensitiveValue("hunter2")
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(TypeError):
        hash(value)
    assert value == SensitiveValue("hunter2") and value != SensitiveValue("x")
    with pytest.raises(ValueError, match="不能为空"):
        SensitiveValue("")


def test_memory_store() -> None:
    store = MemoryCredentialStore({"tws": "pw"})
    assert store.get("tws").reveal() == "pw"
    with pytest.raises(CredentialUnavailableError):
        store.get("missing")


def test_keyring_store_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(service: str, name: str) -> str | None:
        raise NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(CredentialUnavailableError, match="不可用"):
        KeyringCredentialStore().get("tws")
    monkeypatch.setattr(keyring, "get_password", lambda service, name: None)
    with pytest.raises(CredentialUnavailableError, match="缺少"):
        KeyringCredentialStore().get("tws")
    monkeypatch.setattr(keyring, "get_password", lambda service, name: "pw")
    assert KeyringCredentialStore("qts-test").get("tws").reveal() == "pw"


# ---------- 第一轮评审补充 ----------


@pytest.mark.parametrize(
    "key",
    ["dbPassword", "accessToken", "clientSecret", "secretKey", "password2", "tws_password1",
     '"db.password"', '"db password"', "pwd", "pass", "pin", "auth", "cookie", "totp",
     "mnemonic", "signing_key", "encryption_key", "hmac_key", "session_key", "APIKey",
     "PRIVATE_KEY"],
)  # fmt: skip
def test_credential_like_keys_are_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match="看起来是密钥"):
        load_toml(write(tmp_path, "[x]" + NL + f"{key} = 1" + NL), name="t")


def test_credentials_nested_in_arrays_of_arrays_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"x\[0\]\[0\]\.password"):
        load_toml(write(tmp_path, 'x = [[{password = "a"}]]' + NL), name="t")


@pytest.mark.parametrize(
    "key", ["compass", "tokenizer_name", "passive", "bypass", "monkey", "keyboard_layout"]
)
def test_more_harmless_names(tmp_path: Path, key: str) -> None:
    load_toml(write(tmp_path, "[x]" + NL + f"{key} = 1" + NL), name="t")


def test_floats_are_parsed_as_decimal(tmp_path: Path) -> None:
    loaded = load_toml(write(tmp_path, "[risk]" + NL + "max_loss = 0.1" + NL), name="t")
    value = loaded.content["risk"]["max_loss"]
    assert type(value) is Decimal and value == Decimal("0.1")


def test_bom_is_rejected_with_clear_message(tmp_path: Path) -> None:
    path = tmp_path / "bom.toml"
    path.write_bytes(codecs.BOM_UTF8 + b"[x]" + NL.encode())
    with pytest.raises(ConfigError, match="BOM"):
        load_toml(path, name="t")


def test_loaded_config_validates_hash() -> None:
    with pytest.raises(ConfigError, match="sha256"):
        LoadedConfig(name="mode", path="x", sha256="abc", content={})


def test_parsers_check_config_name_and_extra_tables(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="名为 mode"):
        parse_mode_config(load_toml(write(tmp_path, MODE), name="calendar"))
    with pytest.raises(ConfigError, match="只能有"):
        parse_mode_config(load_toml(write(tmp_path, MODE + "[mdoe]" + NL), name="mode"))
    with pytest.raises(ConfigError, match="名为 calendar"):
        parse_calendar_overrides(load_toml(write(tmp_path, CAL), name="mode"))
    with pytest.raises(ConfigError, match="只能有"):
        parse_calendar_overrides(
            load_toml(write(tmp_path, CAL + "[calendr]" + NL), name="calendar")
        )


def test_early_close_during_daylight_saving_time(tmp_path: Path) -> None:
    text = (
        "[calendar]"
        + NL
        + "[[calendar.adhoc_early_closes]]"
        + NL
        + "date = 2026-07-02"
        + NL
        + "close = 13:00:00"
        + NL
    )
    _, early = parse_calendar_overrides(load_toml(write(tmp_path, text), name="calendar"))
    assert early == {date(2026, 7, 2): datetime(2026, 7, 2, 17, 0, tzinfo=UTC)}  # EDT = UTC−4


def test_keyring_backend_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(service: str, name: str) -> str | None:
        raise OSError("backend crashed")

    monkeypatch.setattr(keyring, "get_password", broken)
    with pytest.raises(CredentialUnavailableError, match="OSError"):
        KeyringCredentialStore().get("tws")


# ---------- 第二轮评审补充 ----------


@pytest.mark.parametrize(
    "key", ["dbAuthToken", "authtoken", "apitoken", "wallet_seed", "seed_phrase"]
)
def test_more_credential_names(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match="看起来是密钥"):
        load_toml(write(tmp_path, "[x]" + NL + f"{key} = 1" + NL), name="t")


def test_non_ascii_keys_and_random_seed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="非 ASCII"):
        load_toml(write(tmp_path, "[x]" + NL + '"密码" = "a"' + NL), name="t")
    assert is_credential_key("交易密码") and not is_credential_key("random_seed")
    load_toml(write(tmp_path, "[bt]" + NL + "random_seed = 7" + NL), name="t")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_numbers_are_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="nan 或 inf"):
        load_toml(write(tmp_path, "[risk]" + NL + f"limit = {value}" + NL), name="t")
