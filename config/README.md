# config/

| 路径 | 内容 | 谁能改 |
|---|---|---|
| `config/risk/` | 风控参数与限额（附录 A 草案在 F-04 由 Dylan 批准后落地为 `parameters.yaml`） | 仅 Dylan |
| `config/live/` | 实盘账户、授权、时间窗（G7 之前不存在） | 仅 Dylan |
| 其他 | 非敏感的运行配置、示例文件 | 按任务单 |

`config/risk/` 与 `config/live/` 目前尚未创建，由 Dylan 在 F-04 / G7 时创建。修改流程见
`docs/runbooks/protected-config-change.md`。任何 API 都不能提高限额，AI 无权修改这些文件。
