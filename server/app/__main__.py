"""命令行入口（S-01c）。

python -m server.app {supervise,tradingd,apid,workerd,guardd} [--config 路径] [--root 目录]
python -m server.app token {issue,revoke,list}（令牌管理，见 server.app.tokens）；
--root 是配置里相对路径的基准目录，默认是仓库根目录（测试与多份部署时指定）。

- supervise：守护进程，拉起 tradingd、apid、workerd、guardd 并在崩溃后退避重启。
  由操作系统的服务机制在开机 / 登录时启动它（docs/runbooks/S-01c-services.md）。
- 其余子命令运行单个进程，退出码供守护进程判断是否重启（0 正常、78 拒绝启动、其他为崩溃）。
- B-03（IBKR 适配器）与行情、账户同步数据源接入之前，tradingd 与 guardd 的生产入口
  一律拒绝启动（78），守护进程不会反复拉起它们。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from server.app.apid import ApidSettings, run_apid
from server.app.common import AdapterNotAvailableError
from server.app.guardd import GuarddDeps, GuarddSettings, run_guardd
from server.app.scheduler import Scheduler, ScheduleSettings
from server.app.settings import (
    ApidConfig,
    ProcessSettings,
    parse_apid_config,
    parse_process_settings,
    parse_schedule_settings,
)
from server.app.supervisor import EXIT_REFUSED, ChildSpec, Supervisor
from server.app.tokens import token_main
from server.app.tradingd import (
    TradingdDeps,
    TradingdSettings,
    run_tradingd,
)
from server.app.wiring import Feeds, core_inputs
from server.app.workerd import WorkerdSettings, run_workerd
from server.data.calendar import TradingCalendar
from server.data.calendar_config import parse_calendar_overrides
from server.platforms import LocalChannel, PlatformError, local_channel
from server.risk.assembly import CoreInputs
from server.storage.alertsdb import alerts_db_path
from server.storage.config import ConfigError, load_toml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "runtime" / "processes.toml"
DEFAULT_SCHEDULE = ROOT / "config" / "runtime" / "schedule.toml"
DEFAULT_APID = ROOT / "config" / "runtime" / "apid.toml"
DEFAULT_CALENDAR_OVERRIDES = ROOT / "config" / "runtime" / "calendar_overrides.toml"
CHILDREN = ("tradingd", "apid", "workerd", "guardd")
CHILD_ARGV = (sys.executable, "-m", "server.app")


@dataclass(frozen=True)
class TimeWiring:
    """生产时间来源：一份交易日历、一个券商窗口，调度器与交易核心共用（S-01d 评审 P1-4）。"""

    calendar: TradingCalendar
    schedule: ScheduleSettings

    def scheduler(self, core: Any) -> Scheduler:
        return Scheduler(core, self.calendar, self.schedule)

    def broker_window(self, at: datetime) -> bool:
        window = self.schedule.broker_window
        return window is not None and window.contains(at)

    def inputs(self) -> CoreInputs:
        """交易核心的数据来源：broker_window 与 session_day 必须取自这里（与调度器同源）。

        行情、账户同步等在 B-03 / D 组接入前不可用：拒绝启动。
        """
        raise AdapterNotAvailableError(
            "行情、账户同步等数据源尚未接入（B-03 / D 组），tradingd 拒绝启动；"
            "接入时 broker_window=self.broker_window、session_day=self.calendar.session_day"
        )

    def wire(self, feeds: Feeds, clock: Callable[[], datetime]) -> CoreInputs:
        """按这份日历和券商窗口接好数据来源（B-05w）。生产入口还没有调用它，见 inputs。"""
        return core_inputs(
            feeds,
            broker_window=self.broker_window,
            session_day=self.calendar.session_day,
            clock=clock,
        )


def production_time(
    schedule: ScheduleSettings,
    today: date,
    overrides: Path = DEFAULT_CALENDAR_OVERRIDES,
    *,
    required: bool = False,
) -> TimeWiring:
    """NYSE 交易日历：覆盖前 7 天到后约 13 个月，并叠加临时休市 / 提前收市登记（S-01f）。

    - 登记文件不存在：required（命令行显式指定了路径）时拒绝；否则视为没有登记并记 WARNING；
    - 路径读不了（权限、父路径不是目录等）、格式或内容有误（含试图新增开市、推迟收盘、
      登记在本就休市的日子）、登记晚于覆盖范围（基本是笔误）：
      抛 ConfigError / ValueError，拒绝启动（78）；
    - 早于覆盖范围的旧登记不影响当前日历：忽略并记日志，不因过期的旧登记挡住启动。
    """
    log = logging.getLogger(__name__)
    start, end = today - timedelta(days=7), today + timedelta(days=400)
    closures: frozenset[date] = frozenset()
    early: dict[date, datetime] = {}
    try:
        overrides.stat()
        present = True
    except FileNotFoundError:
        present = False
    except OSError as exc:  # 不能把“读不了”当成“没有登记”
        raise ConfigError(f"无法读取临时日历登记 {overrides}：{exc}") from exc
    if not present:
        if required:
            raise ConfigError(f"指定的临时日历登记文件不存在：{overrides.resolve()}")
        log.warning("没有临时日历登记文件（%s），只用日历库自带的节假日", overrides.resolve())
    else:
        closures, early = parse_calendar_overrides(load_toml(overrides, name="calendar"))
        later = sorted(d for d in {*closures, *early} if d > end)
        if later:
            raise ConfigError(f"临时日历登记晚于覆盖范围（{end} 之后），多半是笔误：{later}")
        older = sorted(d for d in {*closures, *early} if d < start)
        if older:
            log.warning("临时日历登记早于覆盖范围，忽略：%s", older)
        closures = frozenset(d for d in closures if d >= start)
        early = {d: t for d, t in early.items() if d >= start}
    calendar = TradingCalendar.nyse(
        start=start, end=end, adhoc_closures=closures, adhoc_early_closes=early
    )
    return TimeWiring(calendar, schedule)


def child_arguments(
    config: Path, root: Path, schedule: Path, overrides: Path, apid: Path = DEFAULT_APID
) -> tuple[str, ...]:
    """守护进程拉起子进程时透传的参数（绝对路径）：子进程用的配置与守护进程校验过的完全一致。"""
    return (
        "--config", str(config.resolve()), "--root", str(root.resolve()),
        "--schedule", str(schedule.resolve()), "--calendar-overrides", str(overrides.resolve()),
        "--apid-config", str(apid.resolve()),
    )  # fmt: skip


def load(config: Path, channel: LocalChannel, root: Path = ROOT) -> ProcessSettings:
    return parse_process_settings(
        load_toml(config, name="processes"), base=root, me=channel.current_account()
    )


async def _run(  # noqa: PLR0913 - 各进程的配置显式传入
    command: str,
    *,
    settings: ProcessSettings,
    time: TimeWiring,
    channel: LocalChannel,
    child_args: tuple[str, ...],
    apid: ApidConfig | None = None,
) -> int:
    stop = asyncio.Event()
    if command == "tradingd":
        trading = TradingdSettings(
            runtime_dir=settings.runtime_dir, ledger_path=settings.ledger,
            allowed=settings.allowed(), heartbeat_seconds=settings.heartbeat_seconds,
            instance=settings.instance,
        )  # fmt: skip
        deps = TradingdDeps(channel, time.inputs, scheduler=time.scheduler)
        return await run_tradingd(trading, deps, stop)
    if command == "apid":
        if apid is None:
            raise ConfigError("缺少 apid 配置")
        api = ApidSettings(
            runtime_dir=settings.runtime_dir, ledger_path=settings.ledger, api_db=apid.api_db,
            tradingd_account=settings.accounts["tradingd"], host=apid.host, port=apid.port,
            tls_cert=apid.tls_cert, tls_key=apid.tls_key, confirmation_ttl=apid.confirmation_ttl,
            instance=settings.instance, alerts_db=alerts_db_path(apid.api_db),
        )  # fmt: skip
        return await run_apid(api, channel, stop)
    if command == "workerd":
        worker = WorkerdSettings(
            runtime_dir=settings.runtime_dir, meta_path=settings.meta,
            tradingd_account=settings.accounts["tradingd"], max_age=settings.workerd_max_age,
            instance=settings.instance,
        )  # fmt: skip
        return await run_workerd(worker, channel, stop)
    if command == "guardd":
        guard = GuarddSettings(
            runtime_dir=settings.runtime_dir, ledger_path=settings.ledger,
            check_seconds=settings.guard_check_seconds, timeout=settings.guard_timeout,
            alerts_db=None if apid is None else alerts_db_path(apid.api_db),
            api_db=None if apid is None else apid.api_db,
            store_expected=True,
        )  # fmt: skip
        return await run_guardd(guard, GuarddDeps(), stop)
    specs = [ChildSpec(name, (*CHILD_ARGV, name, *child_args)) for name in CHILDREN]
    outcomes = await Supervisor(specs).run()
    logging.getLogger(__name__).warning("守护进程结束：%s", outcomes)
    return 0


def _guardd_apid_config(path: Path, root: Path) -> ApidConfig | None:
    """guardd 只需要 api.db 与 alerts.db 的路径（S-05a 评审 P1-4）：apid 配置读不了也照常看门，
    只是告警改为只在内存里保留（并报 P1），不接受网页确认。"""
    try:
        return parse_apid_config(load_toml(path, name="apid"), base=root)
    except (ConfigError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error(
            "guardd 读不了 apid 配置，告警只在内存里保留、不接受网页确认：%s", exc
        )
        return None


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["token"]:  # 令牌管理（S-03）：不启动任何进程
        return token_main(argv[1:], default_config=DEFAULT_APID, root=ROOT)
    parser = argparse.ArgumentParser(prog="python -m server.app")
    parser.add_argument("command", choices=("supervise", *CHILDREN))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    parser.add_argument("--calendar-overrides", type=Path, default=None)
    parser.add_argument("--apid-config", type=Path, default=DEFAULT_APID)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        channel = local_channel()
        settings = load(args.config, channel, args.root)
        schedule = parse_schedule_settings(load_toml(args.schedule, name="schedule"))
        overrides = args.calendar_overrides or DEFAULT_CALENDAR_OVERRIDES
        today = datetime.now(UTC).date()
        apid = (
            parse_apid_config(load_toml(args.apid_config, name="apid"), base=args.root)
            if args.command in {"apid", "supervise"}
            else _guardd_apid_config(args.apid_config, args.root)
            if args.command == "guardd"
            else None
        )
        time = production_time(
            schedule, today, overrides, required=args.calendar_overrides is not None
        )
    except (ConfigError, PlatformError, ValueError, OSError) as exc:  # 日历数据错误同样拒绝启动
        logging.getLogger(__name__).error("配置或平台不可用，拒绝启动：%s", exc)
        return EXIT_REFUSED
    return asyncio.run(
        _run(args.command, settings=settings, time=time, channel=channel,
             child_args=child_arguments(args.config, args.root, args.schedule, overrides,
                                   args.apid_config), apid=apid)
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
