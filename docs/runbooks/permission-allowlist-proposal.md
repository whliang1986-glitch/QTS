# 权限放行清单（减少“允许”弹窗）

状态：**提案，由 Dylan 本人决定是否采用**（2026-11-04 更新）。Claude 不修改 `.claude/` 下的任何文件，包括它自己的权限设置。

## 给 Dylan：一次性操作（三步）

1. 在电脑上打开项目文件夹 `C:\Users\whlia\Desktop\量化自动交易系统\.claude\`，新建文本文件 `settings.local.json`。
   如果已经有这个文件，就把下面第二节 `allow` 里的条目合并进去。
2. 把第二节的 JSON 整段复制粘贴进去，保存。
   这个文件只在你这台电脑上生效，不会进 git：`.gitignore` 和 Claude Code 默认都会忽略它。
3. 重开 Claude Code 会话。
   **怎么确认生效**：之后 Claude 跑测试、类型检查、`git status` 这类命令时，不再弹“允许”；
   推送、删除、装新依赖、联网下载时仍然会弹。

另外，平时弹窗时如果选“允许并不再询问”（Always allow），Claude Code 会把那一条命令记进同一个 `settings.local.json`，
以后同类命令就不再问。所以即使不做上面三步，提示也会越来越少。但凡是删除、推送、改配置的命令，请继续只点“允许一次”。

## 一、最近实际触发过提示的命令（按类别）

| 类别 | 例子 | 归类 |
|---|---|---|
| 测试与静态检查 | `uv run pytest`、`uv run ruff check / format`、`uv run mypy`、`uv run lint-imports`、`uv run vulture` | 只读或本地验证，**可放行** |
| 项目检查脚本 | `uv run python scripts/arch_scan.py`、`scripts/check_licenses.py`、`scripts/gen_api_types.py` | 本地验证，**可放行** |
| 依赖漏洞扫描 | `uv run pip-audit`、`npm audit` | 只读（会联网查漏洞库，但不改任何东西），**可放行** |
| git 只读 | `git status`、`git diff`、`git log`、`git show`、`git branch`、`git rev-parse`、`git fetch` | 只读，**可放行** |
| git 本地写 | `git add`、`git commit`、`git checkout -b`、`git switch -c`、`git merge --no-edit origin/main` | 只改本地仓库，提交前有 pre-commit 钩子把关，**可放行** |
| 前端验证 | `npx tsc --noEmit`、`npx playwright test`、`npx knip`、`npx vite build` | 本地验证，**可放行** |
| 读文件与查找 | `Get-Content`、`Get-ChildItem`、`Select-String`、`Test-Path`、`Measure-Object` | 只读，**可放行** |
| 推送 | `git push` | 改外部状态，**保持询问** |
| GitHub 操作 | `gh pr ...`、`gh api ...` | 改外部状态，**保持询问** |
| 装依赖、联网下载 | `npm install`、`npm ci`、`uv add`、`uv pip`、`curl`、`Invoke-WebRequest` | 改依赖或下载外部内容，**保持询问** |
| 任意脚本 | `python 某脚本.py`、`python -c ...` | 能做任何事，**保持询问** |
| 删除、覆盖、还原 | `Remove-Item`、`rm`、`git checkout -- 文件`、`git restore`、`git reset` | 可能丢数据，**保持询问** |
| 受保护路径 | 任何读写 `config/risk/`、`config/live/`、`.claude/`、`secrets/` 的命令 | **永远拒绝**（deny 规则加 guard 钩子，与本清单无关） |
| 危险 git | `git push --force`、`git reset --hard`、`git clean`、`git commit --no-verify` | **永远拒绝**（现有 deny 规则） |

## 二、可以直接粘贴的 `settings.local.json`

只有 `allow`，不改现有的 `deny`、`ask` 和钩子（它们在入库的 `.claude/settings.json` 里，继续生效，而且优先于 allow）：

```json
{
  "permissions": {
    "allow": [
      "PowerShell(uv run pytest:*)",
      "PowerShell(uv run ruff:*)",
      "PowerShell(uv run mypy:*)",
      "PowerShell(uv run lint-imports:*)",
      "PowerShell(uv run vulture:*)",
      "PowerShell(uv run pip-audit:*)",
      "PowerShell(uv run python scripts/arch_scan.py:*)",
      "PowerShell(uv run python scripts/check_licenses.py:*)",
      "PowerShell(uv run python scripts/gen_api_types.py:*)",
      "PowerShell(uv sync:*)",
      "PowerShell(uv export:*)",
      "PowerShell(git status:*)",
      "PowerShell(git diff:*)",
      "PowerShell(git log:*)",
      "PowerShell(git show:*)",
      "PowerShell(git branch:*)",
      "PowerShell(git rev-parse:*)",
      "PowerShell(git fetch:*)",
      "PowerShell(git add:*)",
      "PowerShell(git commit -q -m:*)",
      "PowerShell(git commit -m:*)",
      "PowerShell(git checkout -q -b:*)",
      "PowerShell(git checkout -b:*)",
      "PowerShell(git switch -c:*)",
      "PowerShell(git merge --no-edit origin/main:*)",
      "PowerShell(npx tsc:*)",
      "PowerShell(npx playwright test:*)",
      "PowerShell(npx knip:*)",
      "PowerShell(npx vite build:*)",
      "PowerShell(npm audit:*)",
      "PowerShell(Get-Content:*)",
      "PowerShell(Get-ChildItem:*)",
      "PowerShell(Select-String:*)",
      "PowerShell(Test-Path:*)",
      "Bash(uv run lint-imports:*)",
      "Bash(uv run vulture:*)",
      "Bash(uv run python scripts/arch_scan.py:*)",
      "Bash(git fetch:*)",
      "Bash(git branch:*)",
      "Bash(git rev-parse:*)"
    ]
  }
}
```

说明：
- 规则按命令开头匹配。`git commit -m:*` 只放行普通提交；`git commit --no-verify` 仍被 deny 拒绝（deny 优先）。
- `git checkout -b`、`git switch -c` 只放行“新建分支”；`git checkout -- 文件`（丢弃改动）仍会询问。
- `git merge --no-edit origin/main` 只放行“把最新 main 合进当前分支”，不放行合并任意分支。
- 没有放行 `uv run:*`、`python:*`、`npx:*` 这类宽规则：它们能运行任意程序。
- 一行里用分号串了多条命令的，可能仍会弹一次。Claude 这边会尽量一条命令只做一件事，减少这种情况。

## 三、保持询问、绝不放行的

| 命令 | 现状 | 原因 |
|---|---|---|
| `git push` | 每次询问（settings.json 的 `ask`） | 对外发布 |
| `git push --force`、`-f`、`--force-with-lease`，`git reset --hard`，`git clean`，`git filter-branch`，`git commit --no-verify` | 拒绝（`deny`） | 破坏历史或绕过钩子 |
| `rm -rf`、`Remove-Item -Recurse` | 拒绝；其他删除命令询问 | 删除 |
| 读写 `config/risk/`、`config/live/`、`.claude/`、`secrets/`、`.env` | 拒绝（deny 规则加 guard 钩子） | 受保护路径 |
| `npm install`、`npm ci`、`uv add`、`uv pip`、`curl`、`Invoke-WebRequest` | 询问 | 装依赖、联网下载 |
| `gh ...` | 询问 | 改 GitHub 上的状态 |
| 任意 `python` 脚本 | 询问 | 能做任何事 |

## 四、不建议的做法

- 不要用 `PowerShell(*)`、`Bash(*)`、`uv run:*`、`python:*` 这类宽规则。
- 不要关掉或修改 `.claude/hooks/guard_paths.py`：放行规则不会削弱钩子，钩子仍会拦截受保护路径与危险参数。
- 不要把这份清单写进入库的 `.claude/settings.json`：那是全团队共用的安全底线，个人便利放在 `settings.local.json` 里。
