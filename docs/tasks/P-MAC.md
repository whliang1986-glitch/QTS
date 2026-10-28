# P-MAC：macOS 支持

| 字段 | 内容 |
|---|---|
| 任务 ID | P-MAC（V4.1 计划外新增，计划后段） |
| 优先级 | P1 |
| 里程碑 / 门禁 | M2 之后（在 S-06 桌面壳打包之后），实盘前不阻断 |
| 线 | 侧线 |
| 前置任务 | S-01b（平台适配层接口）、S-01c（Windows / Linux 系统服务安装）、S-06（Tauri 桌面壳） |
| 估算工日 | 待估（约 2–3） |
| 状态 | DEFERRED（Dylan 2026-10-28：最终要同时支持 Windows 与 macOS；先做 Windows 与 Linux，macOS 留到计划后段） |

## 目标

让系统能在 macOS 上安装与运行：本机进程间通道、对端身份核验、系统服务、桌面壳都有 macOS 实现，
行为与 Windows / Linux 一致，并在 CI 上验证。

## 非目标

- 不改业务代码；所有差异只放在 `server/platforms` 的 macOS 实现里（业务代码不判断操作系统，由 `tests/arch/test_platform_isolation.py` 强制）。
- 不在此前的任务里为 macOS 留静默降级的路径：在 P-MAC 完成前，`server/platforms/macos.py` 的每个调用都明确抛 `PlatformNotImplementedError`。

## 范围

- `server/platforms/macos.py`：
  - 本机通道：Unix 域套接字，对端 uid 用 `LOCAL_PEERCRED`（`xucred`）或 `getpeereid` 读取，进程号用 `LOCAL_PEERPID`；
  - 套接字放在 tradingd 专属运行目录。
  - 补齐 S-01c 起加入的平台接口：系统服务安装、文件权限、路径约定。
- 系统服务：`launchd`（LaunchAgent / LaunchDaemon plist）安装、卸载、崩溃自动重启；与 Windows 服务、systemd 的语义一致。
- CI：`.github/workflows/ci.yml` 的矩阵加入 `macos-latest`，平台适配层与通道测试在三个系统上都跑。
- 桌面壳：Tauri 苹果版打包、签名与公证（notarization）。
- IB Gateway / TWS 在 macOS 上的安装与 IBC 自动登录说明（runbook）。

## 验收标准

- [ ] `tests/integration/ipc/test_transport.py` 在 macos-latest 上全部通过，不靠跳过。
- [ ] 未登记账户被断开；客户端拒绝冒充的 tradingd；第二个实例不能抢占端点。
- [ ] launchd 安装后 tradingd 崩溃能自动重启，HALT 持久化不被重启清除。
- [ ] 平台隔离架构测试在三个系统上都通过。
- [ ] Tauri 苹果版能连接本机服务并显示断连提示。

## 必须覆盖的失败场景

- 读不到对端凭据 → 断开（失败即关闭）。
- 套接字路径被其他账户或普通文件占用 → 拒绝启动。
- 平台实现缺失 → 明确报错，不退化。
