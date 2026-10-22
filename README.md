# QTS — AI 量化自动交易系统

服务器常驻交易核心 + 瘦客户端。首期：美股与 ETF 现货、日线低频、仅多头、无杠杆，先在 IBKR 模拟盘跑通。
蓝本：[V4.1 开发计划](docs/specs/V4.1-开发计划.md)。协作规则：[CLAUDE.md](CLAUDE.md)。

## 快速开始（Windows）

```powershell
winget install astral-sh.uv          # 一次性
uv tool install pre-commit           # 一次性
winget install Gitleaks.Gitleaks     # 一次性
uv sync
pre-commit install
uv run pytest -q
```

## 状态

M0 准备阶段：F-01（脚手架）、F-02（协作规范与防护）已实现，待验收；任务索引见 [docs/tasks](docs/tasks/README.md)。

本系统不承诺收益；任何策略上线都需人工审批并持续监控。
