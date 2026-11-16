"""实验注册表（T-07）：导出 `server.storage.experiments` 的只追加实验注册实现。"""

from __future__ import annotations

from server.storage.experiments import ExperimentRecord, ExperimentRegistry

__all__ = ("ExperimentRecord", "ExperimentRegistry")
