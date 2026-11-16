# 任务索引

完整任务表见 `docs/specs/V4.1-开发计划.md` 第 13 章。这里列出全部已开任务单的任务，状态以各任务单为准
（`tests/arch/test_task_index.py` 检查每份任务单都在这里、状态一致）。

| ID | 任务 | 级 | 状态 |
|---|---|---|---|
| [F-01](F-01.md) | 项目脚手架与工具链 | P0 | ACCEPTED |
| [F-02](F-02.md) | CLAUDE.md、docs 结构、任务单模板、权限规则与钩子、评审子代理 | P0 | ACCEPTED |
| [F-03](F-03.md) | 架构约束测试（import-linter、唯一下单出口扫描） | P0 | ACCEPTED（CI 结果待核对：提交 172ae7e） |
| [F-04](F-04.md) | 参数草案与 ADR 定稿（Dylan 批准） | P0 | NOT STARTED（部分决定已记录，等 Dylan 逐项确认） |
| [C-01](C-01.md) | 领域模型（含订单类别、已结算/未结算现金） | P0 | ACCEPTED |
| [C-02](C-02.md) | 事件与状态语义（事件模型、转移表、时间与时区约定） | P0 | ACCEPTED |
| [C-03](C-03.md) | SQLite 存储层与迁移（Repository、WAL/FULL、唯一约束、禁改删、命令日志） | P0 | ACCEPTED |
| [C-04](C-04.md) | ModeManager 与实盘切换门禁 | P0 | ACCEPTED |
| [C-05](C-05.md) | 配置版本化与密钥管理 | P1 | ACCEPTED |
| [C-06](C-06.md) | 结构化日志与审计事件 | P1 | ACCEPTED |
| [D-01a](D-01a.md) | DataProvider 接口、契约测试、假数据源与 K 线入湖 | P0 | IN REVIEW；已合并 7cec5b2，待逐项验收（未勾 9 项） |
| [D-01b](D-01b.md) | IBKR 历史数据适配器（不连 TWS 的部分） | P0 | PARTIALLY VERIFIED：契约测试只在替身 IB 上通过，**没有**在 IBKR 上运行 |
| [D-02](D-02.md) | 历史回补与增量更新（DataJob） | P1 | IN REVIEW；已合并 c861672，待逐项验收 |
| [D-03](D-03.md) | 交易日历与时区 | P0 | ACCEPTED（按授权自主验收） |
| [D-04](D-04.md) | 公司行动与复权（版本化） | P1 | IN REVIEW；已合并 82e4817，待逐项验收 |
| [D-05](D-05.md) | 数据质量门禁与报告 | P0 | IN REVIEW；已合并 5f95df5，待逐项验收（未勾 11 项） |
| [D-06](D-06.md) | Parquet/DuckDB 数据湖、目录与保留策略 | P1 | ACCEPTED |
| [B-01](B-01.md) | BrokerAdapter 接口与契约测试套件 | P0 | ACCEPTED |
| [B-02](B-02.md) | FakeBroker 与故障注入（含 SimBroker 撮合内核） | P0 | ACCEPTED |
| [B-03a](B-03a.md) | IBKR 连接安全规则、连接监督器与映射表（不连 TWS） | P0 | IN REVIEW；已合并 7e8ae78，待逐项验收（未勾 8 项） |
| [B-03b](B-03b.md) | IBKR 只读适配器（ib_async 绑定；不连 TWS 的部分） | P0 | PARTIALLY VERIFIED：代码与单元测试完成；**没有**在 TWS 模拟盘联调核对（等 F-05 环境） |
| [B-04](B-04.md) | IBKR 行情订阅与实时/延迟标记（不连 TWS 的部分） | P1 | PARTIALLY VERIFIED：代码与单元测试完成；**没有**在 TWS 模拟盘联调核对 |
| [B-05](B-05.md) | IBKR 模拟盘下单（不连 TWS 的部分） | P0 | PARTIALLY VERIFIED：代码与单元测试完成；**没有**在 TWS 模拟盘联调，契约套件**没有**在 IBKR 上运行 |
| [B-05w](B-05w.md) | 交易核心数据来源接线（只接 FakeBroker） | P0 | IN REVIEW；已合并 7ee2aa7（safety-reviewer 第三轮 APPROVE），待逐项验收；接入 IBKR 前须处理下方残留风险，后续任务 B-05x |
| B-05x | 关键组件校验下沉到风控层（`build_trading_core` 拒绝空清单或缺关键组件，见 [B-05w](B-05w.md) 残留风险） | P0 | NOT STARTED |
| [O-01](O-01.md) | 订单状态机与 UNKNOWN | P0 | ACCEPTED |
| [O-02](O-02.md) | 幂等与成交去重 | P0 | ACCEPTED |
| [O-03](O-03.md) | 撤单与改单竞态 | P0 | ACCEPTED |
| [O-04](O-04.md) | 属性测试与故障注入套件 | P0 | ACCEPTED |
| [O-05](O-05.md) | 崩溃恢复与重连对账 | P0 | ACCEPTED |
| [R-01](R-01.md) | 预交易风控引擎（规则框架、四种结果） | P0 | ACCEPTED |
| [R-02a](R-02a.md) | 风控参数模型与运行时调整（ADR-16 风控面板后端） | P0 | ACCEPTED |
| [R-02b](R-02b.md) | 阈值规则（订单 / 标的 / 账户 / 策略级，含已结算现金与止损冷却期） | P0 | ACCEPTED |
| [R-03](R-03.md) | 数据门禁接入（新鲜度、规格、账户同步） | P0 | IMPLEMENTED（评审 APPROVE；已合并 19421d6，待逐项验收，未勾 0 项） |
| [R-04](R-04.md) | 做空、杠杆、衍生品、市价单默认禁用；保护性止损专项豁免 | P0 | ACCEPTED |
| [R-05](R-05.md) | 急停四级与持久化 | P0 | IMPLEMENTED（评审 APPROVE；已合并 c964063，待逐项验收，未勾 0 项） |
| [R-06](R-06.md) | 券商侧保护性止损与订单预防设置核对 | P1（G3 前置） | R-06a：IMPLEMENTED（已合并 48ac25d，待逐项验收）；R-06b：BLOCKED（等下面的决定） |
| [R-07](R-07.md) | 对账（核心） | P0 | IMPLEMENTED（三轮评审问题已修；已合并 ea04f2f，待逐项验收，未勾 0 项） |
| [R-08](R-08.md) | 运行状态机与健康检查 | P0 | IMPLEMENTED（评审问题已修；已合并 4a62771，待逐项验收，未勾 0 项） |
| [S-01a](S-01a.md) | 交易核心装配（S-01 第 1 步） | P0 | IMPLEMENTED（评审问题已修；三个确认命令已由 S-01a-confirm（f6e184c）实现；已合并 6e3d924，待逐项验收，未勾 0 项） |
| [S-01a-arch](S-01a-arch.md) | 允许交易核心调用三个人工确认入口（arch_scan 最小放宽） | P0 | IN REVIEW；已合并 27afa00，待逐项验收 |
| [S-01a-confirm](S-01a-confirm.md) | 三条人工确认命令接入交易核心 | P0 | IN REVIEW；已合并 f6e184c，待逐项验收 |
| [S-01b](S-01b.md) | 进程间通信与意图待发队列（outbox） | P0 | IN REVIEW（Windows 命名管道与 Linux 套接字已实现；macOS 明确未实现，移交 P-MAC；已合并 36799d0，待逐项验收，未勾 0 项） |
| [S-01c](S-01c.md) | 进程入口、崩溃自动重启、guardd 看门狗 | P0 | IN REVIEW（生产入口在 B-03 / 数据源接入前按设计拒绝启动；已合并 3b0eca3，待逐项验收，未勾 0 项） |
| [S-01c-arch](S-01c-arch.md) | 新增装配层 server/app（依赖规则只加一层） | P0 | IN REVIEW；已合并 e364e00，待逐项验收 |
| [S-01d](S-01d.md) | 调度器 | P0 | IN REVIEW；已合并 febf395，待逐项验收（未勾 0 项） |
| [S-01e](S-01e.md) | 调度加固（S-01d 剩余风险） | P1 | IN REVIEW；已合并 e4f5529，待逐项验收（未勾 0 项） |
| [S-01f](S-01f.md) | 交易日历临时覆盖的加载 | P1 | IN REVIEW；已合并 a77c9ac，待逐项验收（未勾 0 项） |
| [S-02](S-02.md) | REST 与 WebSocket API | P0 | IN REVIEW；已合并 e6492c2，待逐项验收（未勾 9 项） |
| [S-03](S-03.md) | 认证与网络安全 | P0 | IN REVIEW；已合并 55c70ce，待逐项验收（未勾 7 项） |
| [S-04](S-04.md) | Web 客户端核心页 | P0 | IN REVIEW；已合并 df7bbe8，待逐项验收（未勾 3 项） |
| [S-05a](S-05a.md) | 日志与告警页 | P1 | IN REVIEW；已合并 fee1d34，待逐项验收 |
| [S-07](S-07.md) | 手机告警与外部心跳（接口与本地实现） | P0 | IN REVIEW；已合并 d9ce7ac，待逐项验收（未勾 4 项） |
| [S-08](S-08.md) | 备份、异地副本与恢复演练（本机部分） | P0 | IN REVIEW；已合并 2cc5137，待逐项验收（未勾 6 项） |
| [S-09](S-09.md) | ﻿# S-09：诊断包与日终报告 | P1 | S-09a 已合并（d05ae11）；S-09b 已合并（76f8390）；S-09c 已合并（e1509d3）；S-09d 已实现，ACCEPTED |
| [S-12](S-12.md) | 多语言（中文 / 英文） | P1 | IN REVIEW；已合并 97adbca，待逐项验收 |
| [S-13](S-13.md) | 界面视觉设计（计划补充） | P1 | IN REVIEW；已合并 411cbd3，待逐项验收 |
| [S-14](S-14.md) | 自选标的与交易名单入口（计划补充） | P1 | NOT STARTED |
| [S-15](S-15.md) | 情报与影响视图（计划补充） | P1 | NOT STARTED |
| [S-16](S-16.md) | 视觉动效（计划补充） | P2 | NOT STARTED |
| [T-01](T-01.md) | 策略接口与调度 | P0 | IN REVIEW；已合并 64da241，待逐项验收（未勾 5 项） |
| [T-01b](T-01b.md) | workerd 接入策略调度（T-01 移交） | P0 | IN REVIEW；已合并 e770657，待逐项验收（未勾 0 项） |
| [T-02](T-02.md) | 基准策略与链路冒烟策略 | P0 | IN REVIEW（规则已定：集中度上限由 Dylan 选定“保持上限，多持几只”，其余参数由协调方按 Dylan 授权把关；已合并 e8a9e91，待逐项验收，未勾 0 项） |
| [T-04](T-04.md) | 回测引擎（共用 OMS 与风控，SimBroker 撮合） | P1 | BLOCKED：等下面的变更请求，由 Dylan 批准；批准前不写引擎代码 |
| [T-08](T-08.md) | 策略发布门禁（任务单占位，含计划补充） | P1 | NOT STARTED |
| [T-09](T-09.md) | 策略库与生命周期注册表（计划补充） | P1 | T-09a IMPLEMENTED（生命周期规则，纯领域；已合并 4fe9665，待逐项验收）；T-09b NOT STARTED（存储、接入交易核心、API） |
| [T-10](T-10.md) | 因子库（计划补充） | P1 | NOT STARTED |
| [T-10a](T-10.md#7-子任务) | 因子数据模型与注册表（1 工日，前置 T-07） | P1 | NOT STARTED |
| [T-10b](T-10.md#7-子任务) | 因子计算接口与声明式积木（1.5 工日，前置 T-10a、D-07） | P1 | NOT STARTED |
| [T-10c](T-10.md#7-子任务) | 因子手动添加、停用、删除（1 工日，前置 T-10a、S-02） | P1 | NOT STARTED |
| [T-10d](T-10.md#7-子任务) | 因子自动验证流水线（2 工日，前置 T-10b、T-04） | P1 | NOT STARTED |
| [T-10e](T-10.md#7-子任务) | 多重检验校正、报告固化与转正门禁（1 工日，前置 T-10d、T-07） | P1 | NOT STARTED |
| [T-10f](T-10.md#7-子任务) | AI 因子候选通道（0.5 工日，前置 T-10e） | P1 | NOT STARTED |
| [T-11](T-11.md) | 事件驱动策略（计划补充） | P1 | NOT STARTED |
| [A-11](A-11.md) | 实时情报流水线（计划补充） | P1 | NOT STARTED |
| [A-12](A-12.md) | 事件抽取与可信度评估（计划补充） | P1 | NOT STARTED |
| [A-13](A-13.md) | 产业链图谱（计划补充） | P1 | NOT STARTED |
| [A-14](A-14.md) | 影响传导与选股排名（计划补充） | P1 | NOT STARTED |
| [A-15](A-15.md) | 资讯数据源平台管理（计划补充） | P1 | NOT STARTED |
| [H-01](H-01.md) | 工程卫生（无效代码、字体与许可证、漏洞扫描） | P1 | IN REVIEW；已合并 52d7a8d，待逐项验收 |
| [UI-02](UI-02.md) | 指挥中枢布局原型（DEMO 模式） | P2 | IMPLEMENTED；已合并 ec1d458，待逐项验收 |
| [P-MAC](P-MAC.md) | macOS 支持 | P1 | DEFERRED（Dylan 2026-10-28：最终要同时支持 Windows 与 macOS；先做 Windows 与 Linux，macOS 留到计划后段） |

## M3 计划补充：情报驱动交易（2026-11-04）

Dylan 的目标：在风控规则之下，根据实时情报分析市场影响、评估产业链上下游，筛选受影响最大的标的自动交易。
按 Dylan 授权，由 Ada 与协调方决策，直接排入计划。

**依赖顺序**（全部在 M3，排在 A-01 至 A-08 之后）：

```
A-15 数据源注册表 ─→ A-11 情报流水线 ─→ A-12 事件抽取 ─┐
A-06 研究知识库 ─→ A-13 产业链图谱 ────────────────────┼─→ A-14 影响排名（因子，走 T-10 验证）─→ T-11 事件驱动策略
T-09 策略库、T-10 因子库、S-10 待审批队列 ────────────────┘
```

**不变的原则**：
- 新闻、网页、社交媒体内容一律是不可信输入；单一未核实来源不能触发交易（V4.1 9.6）。
- AI 推出的产业链关系必须有数据来源。
- 首期在行业 ETF 层面做，只做多；做空另立项，按 G8 标准，由 Dylan 批准。
- 事件驱动策略逐级晋级：先只出建议（S-10 队列，Dylan 逐笔批准），再模拟盘自动，最后才可能在限额内实盘
  （G5、G6、经济门禁、G7；AI 自主提交还要过 G9）。
- API 密钥只进 Windows 凭据库；付费数据源默认禁用，启用由 Dylan 在界面二次确认。

**自选与交易名单（S-14、S-15，2026-11-04）**：
- 自选只是候选；建议只是提议；只有交易名单里、并通过风控全部限额的意图才下单。
- 交易名单存在账本里，走 ADR-16：加入须 Dylan 二次确认，移出立即生效。硬上限与资格规则放在受保护配置里，由 Dylan 确认。
- 依赖：S-14 依赖 S-04、B-04 与风控面板后端；S-15 依赖 S-14、A-11、A-12、A-14；S-16 在 G4、S-06 之后。

**不影响 G4 路径**：这些任务都在 M3，不在 G4（上线测试）要求的 36 个 P0 任务里。
当前顺序不变：T-02 → IBKR 联调（B-03、B-04、B-05、D-01、F-05）→ 演练与验收 → G4。
