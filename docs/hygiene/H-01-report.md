# H-01 工程卫生报告（2026-11-04）

## 1. 后端无效代码（vulture 2.16，置信度 60% 以上）

扫描范围：`server`、`tests`、`scripts`。只扫 `server` 时，大量只在测试里用到的代码会被误报，
例如 FakeBroker 的故障注入方法，所以三个目录一起扫。共 70 条，逐条核对的结论：

| 类别 | 例子 | 处理 |
|---|---|---|
| 框架调用的函数 | FastAPI 路由（`whoami`）与异常处理、pydantic 校验器（`_consistent`）、SQLAlchemy 事件（`_begin`）、pytest 夹具 | 按装饰器忽略 |
| 框架约定的名字 | `model_config`、`model_post_init`、alembic 的 `down_revision` 等、Decimal 上下文的 `prec`、uvicorn 的 `should_exit`、AST 访问器 `visit_*` | 按名字忽略；迁移目录整体排除 |
| 数据契约字段 | API 返回里的 `retry_same_request_id`、`changed_by`；领域模型的 `base_currency`、`exchange`、`broker_contract_id`、`bid_size`、`ask_size`；风控参数的 `unit`、`description` | 按名字忽略：它们是序列化输出或校验的一部分，代码不直接读 |
| 预留给后续任务 | `RejectReason.OUTSIDE_TRADING_HOURS`、`ORDER_PRECAUTION` | 保留，归 B-05（IBKR 拒单原因映射） |
| | `domain.modes.StrategyVersion` | 保留，归 T-09（策略版本注册表） |
| | `risk.reconcile` 的 `last_report` | 保留，归 S-09（诊断包与日终报告） |
| 真正没用 | `server/strategy/base.py: check_identity`（只是转调 `identity_of`，没有任何调用方） | **已删除** |

结果：配置好忽略项后 vulture 为 0 条，CI 里作为必过项，新增无效代码会直接失败。
以后要新增忽略项，必须在本表补一行，写明原因或归属任务。

## 2. 前端无效代码（knip 5.88.1）

- 生成的 `src/api/types.gen.ts` 不算：它按接口全量生成，没用到的类型是正常的。
- 只在本文件内用到的导出不算问题。
- 剩 1 条：`src/i18n.tsx: useLang`。S-05a 的告警页会用它按语言显示告警。
  所以 knip 暂时只提醒，S-05a 合并后改为必过。

## 3. 后端接口清单

见 `docs/api/ROUTES.md`，由 `tests/unit/api/test_route_inventory.py` 核对与实际路由一致。

| 接口 | 网页在用 | 结论 |
|---|---|---|
| whoami、status、orders、commands、commands/confirmations | 是 | 保留 |
| `GET /v1/orders/{intent_id}` | 否 | 保留：运维排查在用，归 S-05 订单详情 |
| `GET /v1/commands/catalog` | 否 | 保留：命令白名单的自描述，归 S-10 待审批队列 |
| `POST /v1/tokens/{token_id}/revoke` | 否 | 保留：紧急吊销是 S-03 的验收要求，网页入口归 S-05 设置页 |
| `WS /v1/stream` | 否 | 保留：S-02 的验收要求，网页从轮询改为推送归 S-05 |

没有可以删除的接口。接口文档（`docs/api/openapi.json`）与路由一致，由已有的 `tests/unit/api/test_codegen.py` 核对。
S-05a 新增的三个接口（alerts、ack、audit）在 S-05a 合并后补进清单。

## 4. 安全扫描工具的取舍

| 工具 | 结论 | 理由 |
|---|---|---|
| ruff 的 S 规则（bandit 规则集） | 继续用 | 已开启，pre-commit 与 CI 都会跑 |
| bandit | 不加 | ruff 的 S 规则就是从 bandit 移植的，再加一遍只会重复报 |
| semgrep | 暂不加 | 通用规则大多已被 ruff 覆盖；项目自己的关键规则（唯一下单出口、风控不可绕过、受保护路径）已由 `scripts/arch_scan.py` 与 import-linter 强制，比通用规则更准。接入实盘前（G7）再评估一次 |
| pip-audit | 加 | Python 依赖漏洞，CI 必过，每周定时 |
| npm audit | 加 | 生产依赖高危及以上必过，开发依赖只提醒 |
| Dependabot | 加 | 只开 PR，不自动合并 |
| gitleaks | 已有 | 密钥扫描（F-01） |

## 5. 扫描发现的漏洞与处理

| 包 | 原版本 | 问题 | 处理 |
|---|---|---|---|
| pyarrow（Python，运行依赖） | 22.0.0 | PYSEC-2026-113 | 升级到 23.0.1；数据层与集成测试 1078 个通过 |
| vite（前端开发依赖） | 6.3.5 | 开发服务器的文件访问绕过等 3 项 | 升级到 6.4.5 |
| @playwright/test（前端开发依赖） | 1.53.0 | 高危 | 升级到 1.55.1；网页测试 64 例通过 |
| zod（knip 的依赖） | 旧版 | 超大集合校验耗尽内存 | knip 用 5.88.1 |

前端生产依赖（react、react-dom）本来就没有漏洞。开发依赖只在开发机上运行，不进正式构建。

## 6. 字体与许可证

- 字体：见 `docs/licenses/FONTS.md`。现状是不打包、不外链，用系统字体。
- 依赖许可证：见 `docs/licenses/THIRD_PARTY.md`。强传染许可 0 个，需确认 0 个；
  MPL-2.0 有 3 个（certifi、hypothesis、pathspec），不改它们的源码就可以使用。

## 合并 main（S-05a 之后）补充

- vulture：S-05a 的 `AckResponse.requested`、`already` 是接口返回字段，按数据契约字段登记忽略。
- vulture：S-09b 的 `DiagnosticsPreview.expires_in_seconds` 是接口返回字段（代码里只赋值、由客户端读取），按数据契约字段登记忽略。
- 接口清单补上 `GET /v1/alerts`、`POST /v1/alerts/{alert_id}/ack`、`GET /v1/audit`。
- knip：`useLang` 已被告警页使用，现在 0 条，CI 改为必过。
