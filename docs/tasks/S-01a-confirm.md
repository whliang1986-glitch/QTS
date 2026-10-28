# S-01a-confirm：三条人工确认命令接入交易核心

| 字段 | 内容 |
|---|---|
| 任务 ID | S-01a-confirm（S-01a 的后续小改动，依赖 S-01a #16 与 S-01a-arch #17） |
| 优先级 | P0 |
| 状态 | IN REVIEW |

## 改动

`server/risk/core.py` 的命令表新增三条命令，经 apid → IPC → `TradingCore.command()` 执行：

| 命令 | 参数（必须恰好是这些键） | 转给 | 返回 |
|---|---|---|---|
| `cancel_protective_stops` | `confirmation` | `EmergencyControls.cancel_protective_stops` | 每张止损单的撤单请求状态 |
| `emergency_flatten` | `preview_id`、`confirmation` | `EmergencyControls.emergency_flatten` | 每个标的的风控结果、本地状态或错误 |
| `adopt_baseline` | `confirmation` | `Reconciler.adopt_broker_positions` | 写入的调整号 |

- 确认短语与人工操作者由急停与对账模块核对，交易核心不改它们的判断：
  - 撤保护性止损、紧急平仓执行：本次在 `server/risk/emergency.py` 补上人工操作者校验（程序身份
    tradingd、guardd、apid、workerd、system 等一律拒绝；评审 P1：确认短语是公开的，挡不住程序冒用）；
    按 R-05 设计这两条不要求先 HALT（紧急平仓的预览与执行另有交易时段、保护性止损、预览过期 / 一次性 / 内容复核）；
  - 采用基准：对账模块原本就要求 HALT 与人工操作者。
- 操作者目前由调用方自报：真正的身份认证在 apid（S-02 / S-03）。
- 三条命令都在交易核心的锁内执行，与意图、券商回报、对账串行。
- 参数缺键、多键：入口拒绝（IntakeError），不执行、不写命令日志。
- 短语不符等被拒结果写入命令日志（ok=false），同一 request_id 重试返回原结果；成功结果重试也只返回原结果，不会再撤一次、再平一次。

## 测试（先写测试，再实现）

- `tests/integration/risk/test_core.py`（真实账本 + FakeBroker + 真实急停 / 对账模块）：
  - 撤止损：短语不符被拒、止损仍在；正确短语撤单；重试是回放，审计里只执行一次。
  - 紧急平仓：HALT 后预览 → 短语不符被拒 → 正确短语执行；重试回放；同一预览换 request_id 再执行被拒；平仓意图只有一张。
  - 采用基准：短语不符、非人工操作者（tradingd）都被拒；人工确认后对账通过、可恢复。
  - 参数形状：六种缺键 / 多键都拒绝且不记录。
  - 串行化：持有核心锁时三条命令都不执行，释放后才执行。
- `tests/arch/test_order_exit_scan.py`：
  - 新增违规用例：`server/ipc` 直接调用紧急平仓、采用基准，或用字符串分派表引用撤止损入口，都被报告。
  - 新增真实代码实证：`server/` 下引用这三个入口与两个确认短语的文件只有 `risk/emergency.py`、`risk/reconcile.py`、`risk/core.py`（以及将来的 `server/api`）。
- 新增程序身份用例：模块层 tradingd / guardd / System / apid / workerd 五种，核心层 tradingd / guardd，撤止损与平仓都被拒、止损仍在、无平仓意图。
- 变异测试：core.py 9 个变异（去掉参数校验 ×2、操作者写死、不持锁 ×2、参数校验放宽为子集、结果视图丢字段 ×3）、
  emergency.py 3 个变异（两处人工校验换回只查非空、去掉大小写归一），12 个全部被杀死，源文件哈希核对已还原。

## 评审记录

- safety-reviewer（全新上下文，只读）：REQUEST CHANGES，P1 一项、P2 五项。
  - P1 撤止损 / 紧急平仓不拒绝程序身份 → 已修（emergency.py `_require_human`），模块层与核心层各补测试。
  - P2 arch 实证测试的正则漏掉 `obj.method` 写法 → 已修，改为剔除来源标识字面量后整词匹配，并加正反用例。
  - P2 任务单“HALT 前提”表述不准 → 已改。
  - P2 动作已执行但命令日志没写成的窗口 → 记入剩余风险。
  - P2 命令名与受保护入口同名，S-01b 以后在 ipc 里写字面量会触发 dynamic-name → 接受：ipc 只透传命令名不写字面量，
    apid 属于允许位置；规则方向是更严，不放宽。
  - P2 确认短语随指纹写入命令日志 → 可接受（不是凭据）。

## 剩余风险

- 动作已执行、但结果视图或命令日志写入失败时，异常外抛、不记录：同一 request_id 重试时，紧急平仓会因预览已被领取
  而记为“没有这个平仓预览”（实际平仓单已提交，不会重复平仓）；撤止损会再发一轮撤单请求（OMS 对已在撤的单拒绝或幂等）。
  操作者应以订单与审计为准；S-01c 接入进程时考虑把命令日志写失败纳入 SAFE HALT。

## 回滚

还原本提交即可：三条命令从命令表消失，调用会得到“未知命令”的入口拒绝；arch 规则不受影响。
