# config/

| 路径 | 内容 | 谁能改 |
|---|---|---|
| `config/risk/` | 风控参数与限额（附录 A 草案在 F-04 由 Dylan 批准后落地为 `parameters.toml`，模板见 `docs/runbooks/risk-parameters.template.toml`） | 仅 Dylan |
| `config/live/` | 实盘账户、授权、时间窗（G7 之前不存在） | 仅 Dylan |
| 其他 | 非敏感的运行配置、示例文件 | 按任务单 |

`config/risk/` 与 `config/live/` 目前尚未创建，由 Dylan 在 F-04 / G7 时创建。修改流程见
`docs/runbooks/protected-config-change.md`。AI 无权修改这些文件。

风控面板（ADR-16）只能在 `parameters.toml` 写明的 hard_min / hard_max 范围内调整：调严立即生效，调松须 Dylan 本人
二次确认；任何 API 都不能越过这个范围。想强制某个值生效，收窄 hard_min / hard_max 即可（重启后越界的保存值取更严一端）。
修改配置文件后，之前确认过的调松一律作废，需重新确认。
