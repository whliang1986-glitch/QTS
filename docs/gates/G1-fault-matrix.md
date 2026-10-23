# G1 故障场景矩阵（V4.1 11.2，M1 范围 16 项）

G1 通过条件之一：11.2 中 M1 范围的 16 项全部通过（第 17 项“恶意新闻文本”属于 M3，在 G9 验证）。
本表随任务推进更新；“已覆盖”指有自动化测试断言该场景下系统的行为，“待覆盖”列出负责的任务。
所有测试都基于 FakeBroker（B-02）与真实 SQLite 账本，**不代表 IBKR 的真实行为**；IBKR 差异见
`docs/tasks/B-02.md` 的待实测清单，B-05 联调时逐项复核。

| # | 场景 | 期望行为 | 状态 | 证据（测试） |
|---|---|---|---|---|
| 1 | 已接单但响应丢失（超时） | 进入 UNKNOWN，查单后不重发 | 已覆盖 | `tests/fault/test_oms_faults.py::test_s01_*`、随机故障组合；`tests/integration/oms/test_order_manager.py` 中 lost_ack / not_found / place_order_exception 系列 |
| 2 | 成交回报重复两次 | 只入账一次 | 已覆盖 | `test_s02_duplicate_execution_booked_once`；O-02 属性测试（随机重复 + 打乱） |
| 3 | 成交回报乱序（成交先于确认） | 状态正确推进 | 已覆盖 | `test_s03_fill_before_ack`；O-03 随机竞态测试（回报暂存、逆序放出） |
| 4 | 撤单过程中成交、部分成交后撤单 | 成交正确入账，终态一致 | 已覆盖 | `test_s04_*`（含撤单确认先于成交到达）；O-03 两个随机竞态测试 |
| 5 | 拒单：价格偏离、无权限、合约未找到 | REJECTED，不重下 | 已覆盖 | `test_s05_rejections[*]`（拒单后同一决定重试不重下、新决定拒绝签发）；合约未找到在 IBKR 上的报错方式待 B-05 核实 |
| 6 | 提交后进程崩溃 | 重启后先查单，不重发 | 已覆盖（O-05） | `tests/integration/oms/test_recovery.py`（下单前 / 后崩溃、崩溃期间成交、挂起撤单与未定改单跨重启、对账未完成拒绝新单）；随机故障组合 `crash` |
| 7 | 断线重连期间状态漂移 | 重连后对账 | 已覆盖（OMS 部分，O-05）；外来成交与持仓对账待 R-07 | `test_recovery.py`（断线期间成交、撤单、到期，断线时撤单的补发，活动订单查不到时升级）；`test_s07_inflight_reports_lost_across_restart_are_reconciled`；随机故障组合 `drop` / `restart`。FakeBroker 查单返回该 orderRef 的全部历史成交；IBKR 的 reqExecutions 只给当天成交、reqCompletedOrders 有保留期，跨日停机时可能查不全——OMS 此时按成交量不符升级（`reconcile_fill_mismatch`），实际行为待 B-05 核实 |
| 8 | 券商重启窗口内的下单请求 | 被拒，不留半条签发 | 已覆盖（OMS 部分） | `test_s08_restart_window_refuses_new_orders`、随机故障组合（窗口内下单、超时、重试）；窗口状态管理在 B-03；窗口后重新下单时风控决定是否仍然新鲜由 R-01 负责 |
| 9 | 行情陈旧或缺失 | 拒绝新增 | 待覆盖（R-01 / D-05） | — |
| 10 | 风控模块无响应 | 不放行 | 待覆盖（R-01） | — |
| 11 | 账户持仓与券商不一致 | SAFE HALT | 待覆盖（R-07） | 券商侧注入已就绪（B-02 `adjust_position`） |
| 12 | 备份恢复后首次启动 | 先对账，保持 HALT | 已覆盖（对账部分 O-05；HALT 持久化 C-04） | `test_recovery.py::test_restored_backup_is_reconciled_before_new_orders`、`test_backup_without_issuance_does_not_resubmit`（备份之后的签发丢失：首次签发前按 orderRef 查重，券商已有则拒绝并升级）；账本里完全没有的券商订单由 R-07 对账发现；`tests/integration/risk/test_mode_manager.py::test_halt_survives_restart`；启动顺序在 S-01 |
| 13 | 夏令时切换日与提前收市 | 日历正确 | 已覆盖（日历层；调度在 S-01） | `tests/unit/data/test_calendar.py`（2026-11-01 夏令时结束、2027-03-14 开始、2026-11-27 提前收市、临时覆盖） |
| 14 | 磁盘写满或数据库锁 | 账本写失败时失败即关闭 | 部分覆盖 | `tests/integration/storage/test_ledger.py::test_write_failure_propagates`；`test_ledger_failure_before_place_places_nothing`（签发写失败不下单）；数据库锁（SQLITE_BUSY）与磁盘写满未单独测试；进程级处理在 S-01 |
| 15 | tradingd 心跳丢失 | guardd 只撤开仓单，保护性止损保留；outbox 意图不重复成交 | 待覆盖（G-01 / S-01） | 券商侧独立 clientId 已就绪（B-02） |
| 16 | 拆股或分红后持仓与现金变化 | 识别为公司行动，止损同步调整，不误入 SAFE HALT | 待覆盖（R-07 / D-04） | 券商侧注入已就绪（B-02 `apply_split` / `apply_dividend`） |

## 随机故障组合

`tests/fault/test_oms_faults.py::test_random_fault_sequences_keep_oms_invariants`（hypothesis，每次运行 60 个随机
序列 + 15 个固定序列，每个序列 1–25 步，每一步的目标意图随机选取）。

**注入的故障**：提交、上游重试（同一决定 / 新决定）、丢 ACK、丢请求、拒单、成交（1 股 / 4 股）、重复成交、
回报暂存与（逆序）放出、撤单与确认 / 拒绝撤单、切换人工确认撤单、超时、查证、外来成交、券商重启窗口（进入 / 退出）、
当日单到期、连接中断（`drop`）、进程崩溃（`crash`：新 OrderManager 接管同一账本）。断线期间所有故障照常注入：
券商侧事实继续变化而回报丢失，重连后由对账（O-05）补回。

**固定序列**（10 个）：窗口内超时（ACK 已到 / ACK 丢失后重连查证并重试）；暂存回报后进入窗口；窗口内下单被拒后重试；
人工撤单被拒后再撤；提交中 / UNKNOWN 时撤单（挂起，确认后补发）；回报暂存期间券商已到期或已全部成交再撤单；
在途订单的重复成交；（O-05）断线期间成交与到期、断线时撤单、UNKNOWN 时挂起撤单后崩溃、丢请求后崩溃再下单、
崩溃期间全部成交。

**收尾（settle）**：退出窗口并重连（CONNECTED 触发重连对账）→ 放出暂存回报并处理 → 时钟前进 60 秒、超时转 UNKNOWN → 每个意图查证到升级
所需轮数（与 `max_lookup_rounds` 一致，挂起的撤单在查证后补发）→ 再处理一次回报。
暂存时的客户端已断开或已被崩溃替换时，放出的回报丢失，只能靠对账补回（O-04 记录这个缺口的 xfail 用例在 O-05
转为通过：`test_s07_inflight_reports_lost_across_restart_are_reconciled`）。收尾不做外来成交与持仓对账（R-07），
也不处理改单（本测试不改单，改单竞态由 O-03 的随机测试覆盖）。

**断言的不变量**（事实一律直接读 FakeBroker 服务端，不经 OMS 的连接）：

1. 每个意图在券商侧最多一张订单；券商侧的每张单都来自本系统的一次签发；一条订单链只有一次 SUBMIT_SENT（从不重发）；
2. 上游用同一决定重试不改变状态、不下单；用新决定重试一律拒绝签发；窗口内的下单被拒且不留签发；
3. 券商有订单时：OMS 有效成交量 = 券商成交量；状态与券商状态严格对应（撤单中只对应 CANCEL_PENDING）；
   有成交时不可能停在 SUBMITTED；
4. 券商查无此单时 OMS 只停在 UNKNOWN，且已升级（on_unresolved），从未出现过 REJECTED；
5. 撤单已发出（或挂起后补发）且券商没有拒绝时，券商侧订单不能还处于已接单 / 部分成交（活性）；
6. 外来成交只进未匹配表（原因只有 `no_order`）；没断过线时条数等于注入次数，断过线时不多于注入次数
   （断线期间的外来成交回报丢失，由 R-07 持仓对账发现）；
7. 差异种类限定在白名单内：`submit_raised`、`unknown_unresolved`、`unmatched_execution`、`fill_after_terminal`、
   `status_after_terminal`（后两种只在逆序放出时出现）、`cancel_raised`（断线时撤单）、
   `recovered_submitting`（提交途中断线或崩溃）、`cancel_resent`（断线期间的撤单在重连后补发）；暂存过回报时另允许 `cancel_unknown_order`
   （OMS 还没看到终态就撤单，券商报订单不存在，属正常撤单竞态）；非法转移、超额成交、成交冲突、permId 冲突、
   同一 orderRef 下多张单、查单事实不符等任何其他差异都使测试失败。

## 变异检查

逐个把缺陷注入 `server/oms/manager.py`，运行 `tests/fault`，确认测试失败后恢复原文件
（脚本在本地临时目录运行，不入库；2026-10-23，O-04）：

| # | 注入的缺陷 | 结果 | 捕获它的测试 |
|---|---|---|---|
| 0 | 超时不转 UNKNOWN：`check_timeouts` 开头直接 `return []` | 检出 | `test_s01_*` |
| a | 查无此单映射为 REJECTED：`_verified_record` 查不到记录时改为 `_apply(E.BROKER_REJECTED)` | 检出 | `test_s01_lost_request_*`、随机测试 |
| b | 成交不去重：`_book_execution` 入账时给 execution_id 加上已入账条数后缀 | 检出 | `test_s02_*`、随机测试（第一轮随机测试未检出，补了固定序列“在途订单的重复成交”） |
| c | UNKNOWN 或终态时丢弃成交回报：`on_broker_event` 收到成交时，若意图处于 UNKNOWN 或终态直接返回 | 检出（终态部分） | `test_s04_fill_reported_after_cancel_*`；集成测试 3 项。UNKNOWN 部分不构成缺陷：查证时会按查单结果补记成交 |
| d | 挂起的撤单在确认后不补发：`_after_active` 开头直接 `return` | 检出 | 随机测试（固定序列：提交中 / UNKNOWN 时撤单）；集成测试 2 项 |
| e | 签发不幂等：`_check_submission` 已签发时返回 None 并跳过状态检查，`commit_submission` 未落账也继续下单 | 检出 | `test_s01_lost_ack_*`、`test_s05_*`、随机测试 |
| f | 外来成交记到本系统意图：`_intent_for` 在 orderRef / 订单号缺失时返回最近一次提交的意图 | 检出 | `test_foreign_execution_*`、随机测试 |
| g | UNKNOWN 时照抄状态回报：`_on_status` 在 UNKNOWN 时不查证，直接按回报状态记 BROKER_STATUS_OBSERVED | 检出 | `test_s01_status_report_alone_does_not_resolve_unknown`（第一轮未检出，补了这条测试） |

c、d 的“检出”包含 `tests/integration/oms`；其余只跑 `tests/fault` 即检出。

O-05（2026-10-23）：同样逐个注入到 `server/oms/manager.py`，运行 `tests/integration/oms/test_recovery.py` 与
`tests/fault`，14 项全部检出（p–u 为评审后补充）：

| # | 注入的缺陷 | 结果 | 捕获它的测试（举例） |
|---|---|---|---|
| h | 重连不触发对账：`on_broker_event` 收到 CONNECTED 时不调用 `recover` | 检出 | 13 项（含 s07、随机测试） |
| i | 提交途中崩溃的 SUBMITTING 不先转 UNKNOWN | 检出 | `test_crash_before_order_reached_broker_*`、随机测试 |
| j | 活动订单对账不补记查到的成交 | 检出 | `test_fills_while_process_was_down_*`、`test_fills_lost_during_disconnect_*`、s07、随机测试 |
| k | 断线期间的撤单在重连后不补发 | 检出 | `test_cancel_sent_while_disconnected_*`、随机测试 |
| l | 挂起撤单不持久化（`_parked_cancels` 返回空集） | 检出 | `test_parked_cancel_survives_restart`、随机测试 |
| m | 对账未完成也放行新单（去掉提交前的对账检查） | 检出 | 落账前的同步复核随即拒单，32 项失败 |
| n | 对账失败也算完成（忽略逐单失败标记） | 检出 | `test_lookup_failure_for_unknown_order_*`、`test_fill_quantity_mismatch_*`、`test_resent_cancel_refused_*` |
| o | 重启后不查证未定的改单 | 检出 | `test_amendment_in_flight_at_crash_is_resolved_on_restart` |
| p | 对账期间断线不作废（只去掉连接代数一半条件，保留“仍连着线”检查） | 检出 | `test_disconnect_during_recovery_voids_it`（查单中途断线后立即重连） |
| t | 提交落账前不复核连接代数 | 检出 | `test_disconnect_while_issuing_refuses_the_submission` |
| u | 一张订单对账失败即中止整个对账 | 检出 | `test_one_failing_order_does_not_block_cancels_of_others` 等 3 项 |
| q | UNKNOWN 订单查单失败仍算对账完成 | 检出 | `test_lookup_failure_for_unknown_order_fails_recovery` |
| r | 首次签发前不按 orderRef 查重 | 检出 | `test_backup_without_issuance_does_not_resubmit` |
| s | 券商成交量与账本对不上时不升级 | 检出 | `test_fill_quantity_mismatch_fails_recovery_and_escalates` |

## 已知限制

- 测试直接读 FakeBroker 服务端的内部查询（`_lookup`、`_open_orders`、`_completed_orders`）；以后在 FakeBrokerServer
  上提供公开的只读查询接口（生产代码，放到后续任务）。
- 外来成交与持仓、现金的对账不在 OMS（R-07）：断线期间的外来成交回报丢失后，只有持仓对账能发现。
- 随机测试不含改单与崩溃的组合；改单跨重启由 `test_amendment_in_flight_at_crash_is_resolved_on_restart` 覆盖。
- 拒单原因 `CONTRACT_NOT_FOUND` 在 IBKR 上可能在下单前由合约解析报出，而不是作为订单拒绝回报（B-05 联调核实）。
- 随机序列由 hypothesis 生成，每次运行 60 个（无固定种子；失败序列会存入本地 `.hypothesis/` 并在下次优先重放）。
  G1 签字时附最近一次 CI 的运行记录。
