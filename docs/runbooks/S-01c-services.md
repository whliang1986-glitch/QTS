# 后台进程的安装与开机自启（S-01c）

适用：Dylan 本机（Windows）。macOS 支持在 P-MAC 任务里补，Linux 只用于 CI。

> **现在还不需要做。** 在 B-03（IBKR 适配器）和行情 / 账户同步数据源接入之前，tradingd 和 guardd 会主动拒绝启动
> （退出码 78），守护进程也不会反复拉起它们。等上模拟盘（G3）前，我会提醒你按这份说明操作一次。

## 它是怎么工作的

- 只需要让系统在你登录时启动**一个**程序：守护进程 `python -m server.app supervise`。
- 守护进程再拉起 tradingd（交易核心）、workerd（策略与意图投递）、guardd（看门狗）。
  apid（界面后台）在 S-02 加入。
- 任何一个子进程崩溃，守护进程按 1 秒、2 秒、4 秒……最长 60 秒的间隔自动重启它。
  稳定运行 5 分钟后，间隔从头算。
- 子进程如果判断“不能启动”（配置错、账户不符、持久化的 HALT 等），会以 78 退出。守护进程不会重启它，
  日志里会写明原因，需要你看一眼。
- tradingd 被重启后，一定先向券商查单、再对账，然后才接受新单。tradingd 挂掉期间，
  guardd 在心跳中断 90 秒后逐单撤销未成交的开仓单，不碰减仓单和保护性止损。

## 单写者与文件

- 账本 `data/ledger.db` 只有 tradingd 写。guardd 以只读方式打开。
- 运行目录 `data/run/` 下有单实例锁：
  - tradingd.lock、workerd.lock、guardd.lock：同一时刻每种进程只能有一个。重复启动会被拒绝。
  - 进程崩溃时，锁由操作系统自动释放，不用手工删除。
- `data/` 目录在仓库根目录下，已被 git 忽略，不会被提交。
- 本机桌面版三个进程都以你的 Windows 账户运行，进程之间的角色区分只能防程序出错，**不是安全边界**。
  上实盘前要不要给它们分开账户，到时请你决定（见 docs/tasks/S-01b.md）。

## Windows：用“任务计划程序”在登录时启动

1. 先手工试一次（项目文件夹里打开 PowerShell）：

   ```powershell
   uv run python -m server.app supervise
   ```

   能看到三个子进程的启动日志即可，按 Ctrl+C 结束。

2. 打开“任务计划程序”→ 右侧“创建任务…”。
   - **常规**：名称写 `QTS 守护进程`；选“只在用户登录时运行”；**不要**勾“使用最高权限运行”。
   - **触发器**：新建 → “登录时”→ 选你自己的账户。
   - **操作**：新建 → “启动程序”。
     - 程序：`uv` 的完整路径。在 PowerShell 里运行 `(Get-Command uv).Source` 可以查到。
     - 参数：`run python -m server.app supervise`
     - 起始于：项目文件夹的完整路径，例如 `C:\Users\<你的用户名>\Desktop\量化自动交易系统`
   - **条件**：取消“只有在计算机使用交流电源时才启动”（笔记本用电池时也要运行）。
   - **设置**：
     - 勾“如果任务失败，按以下频率重新启动”，间隔 1 分钟，最多 999 次。这是守护进程本身的兜底；
       子进程的重启由守护进程负责。
     - “如果此任务已经运行”选“请勿启动新实例”。
3. 确定保存。注销再登录一次，在任务管理器里能看到几个 `python` 进程即成功。

## 停止

- 在任务计划程序里右键该任务 →“结束”。Windows 上这等于强制结束，tradingd 会按崩溃处理：
  下次启动时先查单、再对账。不会丢单，也不会重复下单。
- 如果只想停止下新单，用界面上的急停（stop_new_orders），不要直接结束进程。

## Linux（仅 CI / 开发参考）

systemd 用户单元示例（`~/.config/systemd/user/qts.service`）：

```ini
[Unit]
Description=QTS supervisor

[Service]
WorkingDirectory=%h/qts
ExecStart=%h/.local/bin/uv run python -m server.app supervise
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
```

把套接字所在的 `data/run/` 设为只有本账户可访问（`chmod 750`）。
