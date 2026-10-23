# 权限放行清单提案（减少 Bash 确认提示）

状态：**提案，未生效**。Claude 不修改自己的权限设置；由 Dylan 决定是否采用、放在哪里。

## 先说结论：提示多的主要原因

`.claude/settings.json` 里已经放行了 `uv run pytest / ruff / mypy`、`pre-commit run`、`gitleaks`、
`git status / diff / log / show / add / commit`。但 Claude 实际执行的命令大多写成了

```
cd "/c/Users/whlia/Desktop/量化自动交易系统" && export PATH="…uv…:$PATH" && uv run pytest …
```

这类复合命令不匹配单条命令的放行规则，所以每次都会弹提示。原因是 Bash 工具里找不到 `uv`。
所以分两步：

1. **Claude 这边（已开始执行）**：不再加 `cd` 前缀（工作目录本来就是项目根目录），每条命令只做一件事。
2. **Dylan 这边（一次性）**：让 Bash 工具能直接找到 `uv`，并补几条放行规则（见下）。

## 建议加入的规则

建议放在**不入库的个人配置** `.claude/settings.local.json`（只影响你这台电脑，不进 git）。
这个文件在 `.claude/` 下，按受保护路径的规则 Claude 不能写，需要你自己创建；内容整体如下：

```json
{
  "env": {
    "PATH": "C:\\Users\\whlia\\AppData\\Local\\Microsoft\\WinGet\\Packages\\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe;C:\\Users\\whlia\\.local\\bin;${PATH}"
  },
  "permissions": {
    "allow": [
      "Bash(uv run lint-imports:*)",
      "Bash(uv run python scripts/arch_scan.py:*)",
      "Bash(uv run python scripts/check_protected_paths.py:*)",
      "Bash(git fetch:*)",
      "Bash(git merge-base:*)",
      "Bash(git branch:*)",
      "Bash(git rev-parse:*)",
      "Bash(ls:*)",
      "Bash(cat:*)",
      "Bash(head:*)",
      "Bash(tail:*)",
      "Bash(wc:*)",
      "Bash(grep:*)",
      "Bash(sed -n:*)",
      "Bash(find:*)",
      "PowerShell(Get-ChildItem:*)",
      "PowerShell(Get-Content:*)",
      "PowerShell(git status:*)",
      "PowerShell(git diff:*)",
      "PowerShell(git log:*)",
      "PowerShell(uv run pytest:*)",
      "PowerShell(uv run ruff:*)",
      "PowerShell(uv run mypy:*)",
      "PowerShell(uv run lint-imports:*)"
    ]
  }
}
```

说明：
- `env.PATH` 那一行只是让 Bash 工具找到 uv；如果 Claude Code 版本不支持 `${PATH}` 展开，可以去掉这一段，
  改为在 Windows 的用户环境变量里把 uv 目录加进 PATH（重开 Claude Code 生效）。
- `uv sync`、`uv lock`、`uv run pytest/ruff/mypy`、`pre-commit run`、`gitleaks`、`git add/commit` 等已在
  `settings.json` 放行，不用重复。
- 规则按命令前缀匹配；`sed -n` 只放行只读打印，`sed -i`（改文件）仍会提示。

## 继续保留提示或拒绝的（不要放行）

| 命令 | 现状 | 原因 |
|---|---|---|
| `git push` | 每次询问（settings.json `ask`） | 对外发布 |
| `git push --force` / `-f` / `--force-with-lease`、`git reset --hard`、`git clean`、`git filter-branch`、`git commit --no-verify` | 拒绝（`deny`） | 破坏历史或绕过钩子 |
| `rm`、`del`、`Remove-Item`、`rmdir` | 询问；`rm -rf`、`Remove-Item -Recurse` 拒绝 | 删除 |
| `curl`、`wget`、`Invoke-WebRequest`、`pip install`、`uv add`、`uv pip` | 询问 | 联网下载、装依赖 |
| 任何写 `.claude/`、`config/risk/`、`config/live/`、`secrets/` 的命令 | 拒绝（deny 规则 + guard 钩子） | 受保护路径 |
| `python -c …`、`python <脚本>`（任意 Python） | 询问 | 能做任何事，不宜整体放行 |

## 不建议的做法

- 不要用 `Bash(*)`、`Bash(uv run:*)`、`Bash(python:*)` 这类宽规则：`uv run` 能运行任意程序。
- 不要关掉或改动 `.claude/hooks/guard_paths.py`；放行规则不会削弱钩子，钩子仍会拦截受保护路径与危险参数。

## 生效方式

1. 在**你自己的终端**里，在项目的 `.claude/` 目录下新建 `settings.local.json`，粘贴上面的 JSON。
2. 确认 `.gitignore` 里有 `.claude/settings.local.json`（避免入库），Claude Code 默认也会忽略它。
3. 重开 Claude Code 会话。之后单条的测试、检查、只读命令不再提示；推送、删除、装依赖仍会提示。
