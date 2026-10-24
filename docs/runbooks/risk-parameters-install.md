# 风控参数落地（Dylan 本人操作）

风控参数在受保护目录，只能由 Dylan 本人提交（开发方与 AI 无权写入）。建议值与每项理由见
`docs/runbooks/risk-parameters.template.toml`，提交前请先审阅；提交即为批准。

1. 复制：把 `docs/runbooks/risk-parameters.template.toml` 复制为 `config/risk/parameters.toml`（按需修改数值，删掉文件头“建议值”说明亦可）。
2. 提交：`git add config/risk/parameters.toml`，再 `git commit -m "F-04: 批准风控参数"`；受保护路径钩子会要求按 `docs/runbooks/protected-config-change.md` 由本人操作。
3. 验证：运行 `uv run python -c "from pathlib import Path; from server.storage.config import load_toml; from server.risk.parameters import parse_parameters; print(parse_parameters(load_toml(Path('config/risk/parameters.toml'), name='risk-parameters')).version)"`，打印出 `risk-parameters@…` 版本号即加载成功；报错即说明哪一项不合法，风控在修好前会拒绝一切新订单。
