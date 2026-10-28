# S-01a-arch：允许交易核心调用三个人工确认入口（arch_scan 最小放宽）

| 字段 | 内容 |
|---|---|
| 任务 ID | S-01a-arch（S-01a 的附属改动，单独提交以便 Dylan 事后可见） |
| 优先级 | P0 |
| 状态 | IN REVIEW |

## 改动

`scripts/arch_scan.py` 的两个类别只新增 `risk/core.py` 一个文件：

- `emergency-confirm`（撤保护性止损确认、紧急平仓执行、撤止损确认短语）：`risk/emergency.py`、**`risk/core.py`**、`api`；
- `recon-adopt`（采用券商持仓为对账基准、确认短语）：`risk/reconcile.py`、**`risk/core.py`**、`api`。

其他任何位置（ai、strategy、risk 的其他模块、oms 等）照旧报违规；确认短语、人工身份校验（ModeManager /
Reconciler 的非人工操作者拒绝）、命令按 request_id 去重与审计都不变。

## 原因

按设计，这三个操作由人工在界面上发起，命令经 apid 通过本机通道交给 tradingd 的交易核心（`server/risk/core.py`）执行；
apid 不持有 OMS、风控引擎与账本写权限，执行只能在 tradingd 里。原规则只允许 `api` 调用，交易核心无法执行。
不放宽规则的替代做法（在 emergency.py 里包一个换了名字的转发入口）等于躲开扫描，不采用。

## 依据（安全规则放宽须 Dylan 本人批准）

Dylan 2026-10-28 在项目频道的本人答复：

- “关于同意放宽扫描规则，你自己决定选择把，我相信你的判断和选择是最好的”
- “我不会改，你怎样才能自己完成，我给你授权”

开发方第一次尝试时被本机权限系统按“削弱安全规则”拦截，未绕过；Dylan 第二次明确授权后再次尝试，未被拦截。
协调方确认采用本方案（最小放宽）。

## 测试

- tests/arch/test_order_exit_scan.py 新增违规用例：risk 的其他模块（limits、engine、context）、oms、ai、strategy，
  以及文件名近似的 `risk/core_helper.py`，调用这三个入口都仍被报告；新增允许用例：`risk/core.py`。
- `uv run pytest -q tests/arch`：372 passed；`uv run python scripts/arch_scan.py`：通过。

## 回滚

还原本提交即可（交易核心随之不能执行这三个命令）。
