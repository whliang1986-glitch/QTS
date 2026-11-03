"""令牌管理命令（S-03）：python -m server.app token {issue,revoke,list}

- 拿 api.db 的单写者锁：apid 正在运行时拒绝执行（退出码 75），先停止 apid。
  停 apid 不影响交易：tradingd 照常运行，只是客户端暂时连不上。
- 明文令牌只在 issue 时写到标准输出一次；库里只存哈希，丢了只能吊销重发。
- 操作者记为当前系统账户（写入 api.db 审计）。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path

from server.app.apid import api_db_lock
from server.app.settings import parse_apid_config
from server.app.supervisor import EXIT_BUSY, EXIT_OK, EXIT_REFUSED
from server.platforms import PlatformError
from server.platforms.files import InstanceLock, InstanceLockedError
from server.storage.apidb import MAX_TOKEN_DAYS, ApiDb, ApiDbError
from server.storage.config import ConfigError, load_toml


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def token_main(argv: Sequence[str], *, default_config: Path, root: Path) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.app token")
    parser.add_argument("--apid-config", type=Path, default=default_config)
    parser.add_argument("--root", type=Path, default=root)
    actions = parser.add_subparsers(dest="action", required=True)
    issue = actions.add_parser("issue", help="签发令牌（明文只显示这一次）")
    issue.add_argument("--name", required=True)
    issue.add_argument("--role", choices=("owner", "viewer"), required=True)
    issue.add_argument("--days", type=int, required=True, help=f"有效期，1 到 {MAX_TOKEN_DAYS} 天")
    revoke = actions.add_parser("revoke", help="吊销令牌")
    revoke.add_argument("--id", required=True, dest="token_id")
    revoke.add_argument("--reason", required=True)
    actions.add_parser("list", help="列出令牌（不含明文）")
    args = parser.parse_args(argv)
    try:
        config = parse_apid_config(load_toml(args.apid_config, name="apid"), base=args.root)
    except (ConfigError, OSError) as exc:
        _err(f"apid 配置不可用：{exc}")
        return EXIT_REFUSED
    try:
        lock = InstanceLock(api_db_lock(config.api_db)).acquire()
    except InstanceLockedError:
        _err(
            "apid 正在运行（api.db 只能有一个写者）：先停止 apid，"
            "或用 owner 令牌调用 POST /v1/tokens/<编号>/revoke 在线吊销"
        )
        return EXIT_BUSY
    except (PlatformError, OSError) as exc:
        _err(f"拿不到 api.db 的锁：{exc}")
        return EXIT_REFUSED
    try:
        db = ApiDb(config.api_db)
        try:
            return _act(db, args, getpass.getuser())
        finally:
            db.close()
    except ApiDbError as exc:
        _err(f"操作失败：{exc}")
        return EXIT_REFUSED
    finally:
        lock.release()


def _act(db: ApiDb, args: argparse.Namespace, actor: str) -> int:
    if args.action == "issue":
        token_id, token = db.issue(args.name, args.role, days=args.days, actor=actor)
        _out(f"令牌编号：{token_id}")
        _out(f"令牌（只显示这一次，请妥善保存）：{token}")
        return EXIT_OK
    if args.action == "revoke":
        db.revoke(args.token_id, actor=actor, reason=args.reason)
        _out(f"已吊销：{args.token_id}")
        return EXIT_OK
    for info in db.tokens():
        state = info.state(db.now())
        _out(f"{info.token_id}  {info.name}  {info.role}  到期 {info.expires_at}  {state}")
    return EXIT_OK


__all__ = ("token_main",)
