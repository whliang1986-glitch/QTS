"""config/runtime/apid.toml 的解析（S-03）：严格键、端口与确认有效期上限、路径规则。"""

from __future__ import annotations

from pathlib import Path

import pytest
from server.app.__main__ import DEFAULT_APID, ROOT
from server.app.settings import MAX_CONFIRMATION_SECONDS, parse_apid_config
from server.storage.config import ConfigError, load_toml

GOOD = """[apid]
host = "127.0.0.1"
port = 8730
api_db = "data/api.db"
tls_cert = ""
tls_key = ""
confirmation_seconds = 60
"""


def parse(tmp_path: Path, text: str) -> object:
    path = tmp_path / "apid.toml"
    path.write_text(text, encoding="utf-8")
    return parse_apid_config(load_toml(path, name="apid"), base=tmp_path)


def test_repository_config_loads() -> None:
    config = parse_apid_config(load_toml(DEFAULT_APID, name="apid"), base=ROOT)
    assert config.host == "127.0.0.1" and config.tls_cert is None and config.tls_key is None
    assert config.confirmation_ttl.total_seconds() <= MAX_CONFIRMATION_SECONDS


def test_good_config(tmp_path: Path) -> None:
    config = parse(tmp_path, GOOD.replace('tls_cert = ""', 'tls_cert = "keys/c.pem"'))
    assert config.tls_cert == tmp_path / "keys" / "c.pem"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("confirmation_seconds = 60", "confirmation_seconds = 301"),
        ("confirmation_seconds = 60", "confirmation_seconds = 0"),
        ("port = 8730", "port = 0"),
        ("port = 8730", "port = 70000"),
        ("port = 8730", 'port = "8730"'),
        ('host = "127.0.0.1"', 'host = ""'),
        ('api_db = "data/api.db"', 'api_db = "/abs/api.db"'),
        ('api_db = "data/api.db"', 'api_db = "../api.db"'),
        ('api_db = "data/api.db"', 'api_db = "C:/api.db"'),
        ('tls_cert = ""', "tls_cert = 1"),
        ('tls_key = ""', 'tls_key = "../k.pem"'),
        ("confirmation_seconds = 60\n", "confirmation_seconds = 60\nextra = 1\n"),
        ("confirmation_seconds = 60\n", ""),
    ],
)
def test_bad_values_are_refused(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ConfigError):
        parse(tmp_path, GOOD.replace(old, new))


def test_extra_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        parse(tmp_path, GOOD + "\n[other]\nx = 1\n")
