# 运行手册：修改受保护配置（仅限 Dylan）

`config/risk/`（风控参数与限额）和 `config/live/`（实盘账户、授权）只能由 Dylan 本人修改。
Claude Code 对它们只读，`secrets/` 对 Claude Code 完全不可见，且永不入库。

## 三道防线

| 层 | 位置 | 作用 |
|---|---|---|
| 1 | `.claude/settings.json` 的 `permissions.deny` | Claude Code 的 Edit/Write 工具直接被拒 |
| 2 | `.claude/hooks/guard_paths.py`（PreToolUse） | 拦截工具调用与 Shell 命令中的写入、`--no-verify`、`ALLOW_PROTECTED_CONFIG` |
| 3 | `scripts/check_protected_paths.py`（git pre-commit） | 提交里出现这些路径就拒绝；推送到 GitHub 后再由 CODEOWNERS 要求 Dylan 审批 |

已知局限：钩子是按命令文本识别的，不能保证识别所有间接写法（例如脚本内部动态拼路径）。
所以第 3 道防线和 CODEOWNERS 审批不能省略，Claude Code 提交的差异里出现这些路径时要直接退回。

## Dylan 修改步骤

1. 在**自己的终端**（不是 Claude Code）里用编辑器修改文件，例如 `config/risk/parameters.yaml`。
2. 检查差异：`git diff config/risk`。
3. 提交时显式放行（只对这一条命令生效）：

   PowerShell：

   ```powershell
   $env:ALLOW_PROTECTED_CONFIG = "1"; git commit -m "F-04: 批准风控参数 v1"; Remove-Item Env:ALLOW_PROTECTED_CONFIG
   ```

   Git Bash：

   ```bash
   ALLOW_PROTECTED_CONFIG=1 git commit -m "F-04: 批准风控参数 v1"
   ```

4. 在提交信息或 `docs/gates/` 里记录批准版本号与日期。

## secrets/

- 放在 `<DATA_ROOT>/secrets/` 或系统凭据存储（keyring），仅系统服务账户可读。
- 仓库中的 `secrets/` 已写入 `.gitignore`；即使设置了 `ALLOW_PROTECTED_CONFIG` 也无法提交。
- 不要把密钥贴进与 Claude 的对话。
