# apid 接口清单（H-01）

每个 /v1 接口一行：谁在用、哪里有测试、网页是否用到。新增或删除接口必须同时改这张表，
`tests/unit/api/test_route_inventory.py` 会核对它与实际路由（docs/api/openapi.json 加 WebSocket）完全一致。

没人用的接口要么删掉，要么在“归属”里写明哪个任务会用到它。

| 方法 | 路径 | 用途 | 使用方 | 测试 | 网页 | 归属 |
|---|---|---|---|---|---|---|
| GET | /v1/whoami | 当前令牌的使用者与角色 | 网页登录与顶栏 | tests/unit/api/test_app.py | 是 | S-02 |
| GET | /v1/status | 模式与 HALT 状态 | 网页总览、风险页 | tests/unit/api/test_app.py | 是 | S-02 |
| GET | /v1/orders | 最近订单 | 网页订单页 | tests/unit/api/test_app.py | 是 | S-02 |
| GET | /v1/orders/{intent_id} | 订单详情：历史与成交 | 运维排查（curl）；网页订单详情抽屉 | tests/unit/api/test_app.py | 否 | S-05（订单详情） |
| GET | /v1/commands/catalog | 命令白名单自描述 | 运维脚本、S-10 待审批队列 | tests/unit/api/test_app.py | 否 | S-10 |
| POST | /v1/commands/confirmations | 高危命令二次确认 | 网页急停 | tests/unit/api/test_app.py | 是 | S-03 |
| POST | /v1/tokens/{token_id}/revoke | 在线吊销令牌 | 紧急吊销（curl）；网页设置页 | tests/integration/app/test_apid.py | 否 | S-05（设置页） |
| POST | /v1/commands | 急停、撤单、对账、恢复等命令 | 网页急停 | tests/unit/api/test_app.py | 是 | S-02 |
| GET | /v1/alerts | 告警列表：全部未解除的加最近 limit 条 | 网页告警页 | tests/unit/api/test_alerts_api.py | 是 | S-05a |
| POST | /v1/alerts/{alert_id}/ack | 确认告警（只停止重复提醒，仅 owner） | 网页告警页 | tests/unit/api/test_alerts_api.py | 是 | S-05a |
| GET | /v1/audit | apid 审计记录（仅 owner，可翻页） | 网页审计日志页 | tests/unit/api/test_alerts_api.py | 是 | S-05a |
| WS | /v1/stream | 状态与订单事件推送 | 网页从轮询改为推送 | tests/unit/api/test_app.py | 否 | S-05（实时推送） |
