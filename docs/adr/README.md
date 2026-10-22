# 架构决策记录（ADR）

状态以 V4.1 第 2 章为起点，F-04 由 Dylan 逐项确认后更新本表；单项决策需要展开时，按 `TEMPLATE.md`
新建 `ADR-NN-<主题>.md`。

| ID | 决策（摘要） | 状态 |
|---|---|---|
| ADR-01 | 从零开发，不迁移旧 Codex 代码 | 已定 |
| ADR-02 | 服务器常驻交易核心 + 瘦客户端，所有数据存服务器 | 已定 |
| ADR-03 | Python 3.12 + asyncio，FastAPI + Pydantic v2，Polars/pandas | 待确认 |
| ADR-04 | React + TypeScript Web 客户端，可选 Tauri 桌面壳 | 待确认（见下方注 1） |
| ADR-05 | SQLite（WAL、FULL）存账本；Parquet + DuckDB 存行情 | 待确认 |
| ADR-06 | 首家券商 IBKR，ib_async，经 BrokerAdapter 隔离；IBC 自动登录 | 待确认（纪要已定“先接 IBKR、经 BrokerAdapter 隔离”） |
| ADR-07 | 回测数据供应商待选；M1 先用 IBKR 历史数据 | 待确认 |
| ADR-08 | 自研薄回测引擎，复用 OMS、风控与 FakeBroker | 待确认 |
| ADR-09 | 首期日线低频、仅多头、无杠杆、美股与 ETF 现货 + 冒烟策略 | 待确认（纪要已定范围） |
| ADR-10 | AI 后置到 M3，模型供应商可插拔 | 待确认 |
| ADR-11 | 系统服务常驻，不用 Docker；仅局域网监听 | 待确认 |
| ADR-12 | 内部时间一律 UTC；金额与数量用 Decimal | 已定 |
| ADR-13 | Claude Code + CLAUDE.md + CI + 架构测试；P0 双重审阅 | 待确认 |
| ADR-14 | 本机套接字 / 命名管道 IPC + outbox + tradingd 侧去重 | 待确认 |
| ADR-15 | 首期建议现金账户；区分已结算与未结算现金 | 待确认 |

注 1：需求纪要写的是“最终打包成桌面应用；下单核心作为后台服务常驻，桌面应用只做界面”。这与
ADR-04（Web 界面 + Tauri 桌面壳）方向一致，但桌面壳在 V4.1 中是 P2 可选项（S-06）。F-04 时请 Dylan
确认桌面壳是否要提为必选。
