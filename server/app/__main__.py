"""命令行入口（S-01c）。

python -m server.app {supervise,tradingd,workerd,guardd} [--config 路径] [--root 目录]；
--root 是配置里相对路径的基准目录，默认是仓库根目录（测试与多份部署时指定）。

- supervise：守护进程，拉起 tradingd、workerd、guardd 并在崩溃后退避重启（apid 在 S-02 加入）。
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from server.app.common import AdapterNotAvailableError
from server.app.guardd import GuarddDeps, GuarddSettings, run_guardd
from server.app.scheduler import Scheduler, ScheduleSettings
from server.app.settings import ProcessSettings, parse_process_settings, parse_schedule_settings
from server.app.supervisor import EXIT_REFUSED, ChildSpec, Supervisor
from server.app.tradingd import (
    TradingdDeps,
    TradingdSettings,
    run_tradingd,
)
from server.app.workerd import WorkerdSettings, run_workerd
from server.data.calendar import TradingCalendar
from server.platforms import LocalChannel, PlatformError, local_channel
from server.risk.assembly import CoreInputs
from server.storage.config import ConfigError, load_toml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "runtime" / "processes.toml"
DEFAULT_SCHEDULE = ROOT / "config" / "runtime" / "schedule.toml"
CHILDREN = ("tradingd", "workerd", "guardd")
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


def production_time(schedule: ScheduleSettings, today: date) -> TimeWiring:
    """NYSE 交易日历覆盖前 7 天到后约 13 个月。

    受保护配置里的临时休市 / 提前收市登记在 D 组接入前不加载（日历库自带的节假日照常生效）。
    """
    calendar = TradingCalendar.nyse(
        start=today - timedelta(days=7), end=today + timedelta(days=400)
    )
    return TimeWiring(calendar, schedule)


def load(config: Path, channel: LocalChannel, root: Path = ROOT) -> ProcessSettings:
    return parse_process_settings(
        load_toml(config, name="processes"), base=root, me=channel.current_account()
    )


async def _run(  # noqa: PLR0913 - 命令行解析后的全部输入
    command: str,
    *,
    settings: ProcessSettings,
    time: TimeWiring,
    channel: LocalChannel,
    config: Path,
    root: Path,
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
        )  # fmt: skip
        return await run_guardd(guard, GuarddDeps(), stop)
    specs = [
        ChildSpec(name, (*CHILD_ARGV, name, "--config", str(config), "--root", str(root)))
        for name in CHILDREN
    ]
    outcomes = await Supervisor(specs).run()
    logging.getLogger(__name__).warning("守护进程结束：%s", outcomes)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.app")
    parser.add_argument("command", choices=("supervise", *CHILDREN))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        channel = local_channel()
        settings = load(args.config, channel, args.root)
        schedule = parse_schedule_settings(load_toml(args.schedule, name="schedule"))
        time = production_time(schedule, datetime.now(UTC).date())
    except (ConfigError, PlatformError, ValueError) as exc:  # 日历数据错误同样拒绝启动
        logging.getLogger(__name__).error("配置或平台不可用，拒绝启动：%s", exc)
        return EXIT_REFUSED
    return asyncio.run(
        _run(args.command, settings=settings, time=time, channel=channel,
             config=args.config, root=args.root)
    )  # fmt: skip


if __name__ == "__main__":
    sys.exit(main())
