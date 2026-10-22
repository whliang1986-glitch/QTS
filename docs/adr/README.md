# 架构决策记录（ADR）

状态以 V4.1 第 2 章为起点，F-04 由 Dylan 逐项确认后更新本表；单项决策需要展开时，按 `TEMPLATE.md`
新建 `ADR-NN-<主题>.md`。

| ID | 决策（摘要） | 状态 |
|---|---|---|
| ADR-01 | 从零开发，不迁移旧 Codex 代码 | 已定 |
| ADR-02 | 服务器常驻交易核心 + 瘦客户端，所有数据存服务器 | 已定 |
| ADR-03 | Python 3.12 + asyncio，FastAPI + Pydantic v2，Polars/pandas；domain 层模型用 Pydantic v2 | 已定（2026-10-22） |
| ADR-04 | Tauri 原生桌面 App 为主形态（同一套 React 前端），服务独立常驻 | 已定（修订，见 [ADR-04-desktop-client.md](ADR-04-desktop-client.md)） |
| ADR-05 | SQLite（WAL、FULL）存账本，SQLAlchemy Core + Alembic 迁移（不用 ORM）；Parquet + DuckDB 存行情 | 已定（2026-10-22） |
| ADR-06 | 首家券商 IBKR，ib_async，经 BrokerAdapter 隔离；先用 TWS 自动连接（模拟盘端口白名单），IBC 后置；开发期延迟行情开关 | 已定（修订，见 [ADR-06-ibkr-tws.md](ADR-06-ibkr-tws.md)） |
| ADR-07 | 回测数据供应商待选；M1 先用 IBKR 历史数据 | 待确认 |
| ADR-08 | 自研薄回测引擎，复用 OMS、风控与 FakeBroker | 待确认 |
| ADR-09 | 首期日线低频、仅多头、无杠杆、美股与 ETF 现货 + 冒烟策略 | 待确认（纪要已定范围） |
| ADR-10 | AI 后置到 M3，模型供应商可插拔 | 待确认 |
| ADR-11 | 系统服务常驻，不用 Docker；仅局域网监听 | 待确认 |
| ADR-12 | 内部时间一律 UTC；金额与数量用 Decimal | 已定 |
| ADR-13 | Claude Code + CLAUDE.md + CI + 架构测试；P0 双重审阅 | 待确认 |
| ADR-14 | 本机套接字 / 命名管道 IPC + outbox + tradingd 侧去重 | 待确认 |
| ADR-15 | 以 IBKR 返回的账户类型为准；界面只能一键从严，放宽走受保护配置；类型变化进入 SAFE HALT | 已定（修订，见 [ADR-15-account-type.md](ADR-15-account-type.md)） |

注：ADR-04、ADR-06、ADR-15 已按 Dylan 2026-10-22 的决定修订，原 V4.1 条文以修订文件为准。
