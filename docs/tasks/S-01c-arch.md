# S-01c-arch：新增装配层 server/app（依赖规则只加一层）

| 字段 | 内容 |
|---|---|
| 任务 ID | S-01c-arch（S-01c 的前置改动，单独提交以便 Dylan 事后可见） |
| 优先级 | P0 |
| 状态 | IN REVIEW |

## 改动

- 新建 `server/app/`：tradingd / apid / workerd / guardd 的进程入口（组合根），本次只有说明文件，入口在 S-01c 实现。
- `scripts/arch_scan.py`：
  - `KNOWN_LAYERS` 登记 `app`；
  - `BROKER_IMPORT_ALLOWED` 只加 `app`（要构造 IBKR 适配器）；
  - 其他白名单与受保护名称规则不变（`OMS_IMPORT_ALLOWED`、`SDK_ALLOWED`、各类别允许位置都没有 app）。
- `pyproject.toml`（import-linter）：
  - 新增 `app-is-top`：其他 13 个层都不得依赖 app；
  - 新增 `app-no-direct-oms`：app 不得直接导入 oms（OrderManager 由 risk 的装配函数构造，可经 risk 间接用到）；
  - 把 app 补进既有契约：domain 独立、平台层叶子、FakeBroker 只给回测（app 不得导入 broker.fake）；
  - broker 白名单契约的名字改为如实列出 app，内容不变（app 本来就不在禁止列表里）；
  - FakeBroker 契约的名字缩短，以免输出折行（规则不变，原说明挪到注释）。
- 测试辅助：`tests/arch/_layout.py`、`tests/arch/test_platform_isolation.py` 登记 app。

## 依据（安全规则改动须 Dylan 本人批准）

- 第一次尝试被本机权限系统按“削弱安全规则”拦下，未绕过，已撤回半成品。
- Dylan 2026-10-28 在项目频道本人逐字确认：“同意新增装配层 server/app：登记为新分层，只把它加入 broker 导入白名单，
  并新增‘其他模块不得依赖 app’‘app 不得直接依赖 oms’两条约束，其他规则不变”。
- 此前他也答复过“这些事情你自己做主就行”。

## 反例测试

- `tests/arch/test_order_exit_scan.py`：app 里下单、撤单、全局撤单、直接调提交入口、签发订单票据、构造风控决定、执行紧急平仓、
  采用对账基准、冒用止损来源、直接导入 oms、导入 ib_async、动态反射、用字符串取下单方法，都报违规；
  platforms、ipc 导入 broker 仍报违规。允许用例：app 导入 broker.ibkr 构造适配器、导入 risk。
- `tests/arch/test_import_contracts.py`：risk / broker / oms 依赖 app 打破 `app-is-top`；domain、平台层依赖 app 打破各自契约；
  app 直接依赖 oms 打破 `app-no-direct-oms`；app 依赖 FakeBroker 打破 FakeBroker 契约；
  strategy 经 app 间接拿到 broker 仍打破“决策层不得依赖 broker”；ipc 仍不能直接依赖 broker。
  允许用例：app 依赖 broker、经 risk 间接依赖 oms、依赖平台层与通道。
- `tests/arch/test_layer_registry.py`：既有断言（domain、broker 白名单、FakeBroker 契约覆盖全部分层）之外，
  新增 `app-is-top` 的源必须等于“除 app 外的全部分层”、`app-no-direct-oms` 的源与禁止对象、平台层叶子契约覆盖全部其他分层
  ——以后新增分层忘了补这几条契约会直接失败。
- 逐层反例：除 app 外的 13 个分层各自依赖 app，都打破 `app-is-top`。

## 评审记录

safety-reviewer（全新上下文，只读）：APPROVE，无 P0；两项 P1 已在本任务修复：
- P1-1 新约束没有和分层登记表绑定 → 补上登记表断言（见上）。
- P1-2 其他层依赖 app 只测了 3 层 → 改为逐层参数化。
- P2-3 补了 app 专属的绕过写法反例：相对导入 / 动态导入 oms、经 SDK 客户端下单、别名导入撤单。

## 移交（S-01c，来自评审 P2）

- 字面量动态导入（`import_module("server.app…")`、`import_module("server.broker.fake")`）import-linter 看不到，
  arch_scan 也没有专门规则。建议给 arch_scan 加 `app-import`、`fake-import` 两条收紧规则。
  arch_scan 受 CODEOWNERS 保护，会单独提交并报 Dylan。
- risk 只导出装配函数，不再导出 OMS 类，app 不得经 risk 或 backtest 自己构造 OrderManager；写进 S-01c 评审清单。
- 券商适配器不对外暴露底层 SDK 对象（`_ib`、`client`），避免绕过受保护名称直接发原始协议消息。

## 回滚

还原本提交：删除 server/app，KNOWN_LAYERS、BROKER_IMPORT_ALLOWED 与 pyproject 契约恢复原样。
