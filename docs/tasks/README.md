# 任务索引

完整任务表见 `docs/specs/V4.1-开发计划.md` 第 13 章。这里只列已开任务单的任务。

| ID | 任务 | 级 | 状态 |
|---|---|---|---|
| [F-01](F-01.md) | 项目脚手架与工具链 | P0 | ACCEPTED |
| [F-02](F-02.md) | CLAUDE.md、docs、模板、权限规则与钩子、评审子代理 | P0 | ACCEPTED |
| [F-03](F-03.md) | 架构约束测试（import-linter、唯一下单出口扫描） | P0 | ACCEPTED |
| [F-04](F-04.md) | 参数草案与 ADR 定稿（Dylan 批准） | P0 | NOT STARTED（3 项 ADR 已修订定稿） |
| F-05 | 环境联通性验证（IBKR 模拟账户、IBC、实时行情、供应商试用、告警通路） | P0 | NOT STARTED |
| [C-01](C-01.md) | 领域模型 | P0 | ACCEPTED |
| [C-02](C-02.md) | 事件与状态语义 | P0 | ACCEPTED |
| [C-03](C-03.md) | SQLite 存储层与迁移 | P0 | ACCEPTED |
| [C-04](C-04.md) | ModeManager 与实盘切换门禁 | P0 | ACCEPTED |
| [S-02](S-02.md) | REST 与 WebSocket API | P0 | IN REVIEW |
| [T-09](T-09.md) | 策略库与生命周期注册表（计划补充） | P1 | NOT STARTED |
| [T-10](T-10.md) | 因子库（计划补充，约 6.5 工日，T-07 之后、T-09 之前） | P1 | NOT STARTED |
| [T-10a](T-10.md#7-子任务) | 因子数据模型与注册表（1 工日，前置 T-07） | P1 | NOT STARTED |
| [T-10b](T-10.md#7-子任务) | 因子计算接口与声明式积木（1.5 工日，前置 T-10a、D-07） | P1 | NOT STARTED |
| [T-10c](T-10.md#7-子任务) | 因子手动添加、停用、删除（1 工日，前置 T-10a、S-02） | P1 | NOT STARTED |
| [T-10d](T-10.md#7-子任务) | 因子自动验证流水线（2 工日，前置 T-10b、T-04） | P1 | NOT STARTED |
| [T-10e](T-10.md#7-子任务) | 多重检验校正、报告固化与转正门禁（1 工日，前置 T-10d、T-07） | P1 | NOT STARTED |
| [T-10f](T-10.md#7-子任务) | AI 因子候选通道（0.5 工日，前置 T-10e） | P1 | NOT STARTED |
| [T-04](T-04.md) | 回测引擎（计划补充：第二套独立实现复算） | P1 | NOT STARTED |
| [T-08](T-08.md) | 策略发布门禁（计划补充：扣成本跑赢基准、前向模拟盘、偏差跟踪） | P1 | NOT STARTED |
