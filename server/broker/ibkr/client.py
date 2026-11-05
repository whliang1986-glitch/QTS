"""IbClient 的 ib_async 实现（B-03b）：ConnectionSupervisor 与只读适配器共用这一个连接。

ib_async 只能在 server/broker/ibkr 里 import（arch_scan 强制）。**未在模拟盘实测**：
`disconnect` 的幂等、`isConnected` 的语义、账户摘要字段都要在 B-03b 联调时核对。

- 纵深防御：这一层自己也只连本机、只连模拟盘端口、clientId 为正；
  默认只读，非只读要在构造时显式 `allow_orders=True`（只给 B-05 的模拟盘下单适配器用）
  （oms、guard 等包也能 import broker，不能只靠上层的 ConnectionPolicy）；
- ib_async 的 readonly 只决定启动时同步哪些数据，不能在 TWS 侧禁止下单：
  真正的只读靠 TWS 的“Read-Only API”设置（F-05 清单），以及适配器不提供下单路径；
- 只用 ib_async 的 `*Async` 方法：同步便捷方法（如 `ib.connect()`、`ib.accountSummary()`）
  内部会跑事件循环，在服务端会出问题；
- 不调用 ib_async.util.patchAsyncio / startLoop（它们会用 nest_asyncio 改动事件循环）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import ib_async

from server.broker.ibkr.connection import LIVE_PORTS, LOCALHOST, PAPER_PORTS
from server.broker.ibkr.convert import summary_map

CONNECT_TIMEOUT = 10.0  # 秒：握手超时按连接失败处理，由监督器退避重连


class IbAsyncClient:
    """IbClient 协议的实现。持有一个 ib_async.IB，适配器通过 `ib` 取用同一个连接。"""

    def __init__(self, ib: Any | None = None, *, allow_orders: bool = False) -> None:
        self.ib = ib if ib is not None else ib_async.IB()
        # 非只读连接要显式开启（B-05 模拟盘下单）：默认只读，忘了配置时不会意外放开
        self._allow_orders = allow_orders is True

    @property
    def allow_orders(self) -> bool:
        """只读属性：只能在构造时开启。"""
        return self._allow_orders

    async def connect(self, host: str, port: int, client_id: int, *, readonly: bool) -> None:
        if readonly is not True and not (readonly is False and self.allow_orders):
            raise ValueError("非只读连接要在客户端显式开启 allow_orders（只用于模拟盘下单）")
        if host != LOCALHOST:
            raise ValueError(f"只允许连接本机 {LOCALHOST}")
        if type(port) is not int or port in LIVE_PORTS or port not in PAPER_PORTS:
            raise ValueError(f"只允许模拟盘端口 {sorted(PAPER_PORTS)}")
        if type(client_id) is not int or client_id <= 0:
            raise ValueError("clientId 必须是正整数（0 是 TWS 主客户端，不使用）")
        await self.ib.connectAsync(host, port, clientId=client_id, timeout=CONNECT_TIMEOUT,
                                   readonly=readonly)  # fmt: skip

    async def disconnect(self) -> None:
        """未连接、半连上时调用也安全（ib_async 的 disconnect 会释放 socket；幂等待实测）。"""
        self.ib.disconnect()

    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    async def managed_accounts(self) -> Sequence[str]:
        return list(self.ib.managedAccounts())

    async def account_summary(self, account_id: str) -> Mapping[str, str]:
        values = await self.ib.accountSummaryAsync(account_id)
        return summary_map(values, account_id)


__all__ = ("CONNECT_TIMEOUT", "IbAsyncClient")
