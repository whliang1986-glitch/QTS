# 项目自检报告（2026-11-10）

范围：仓库结构、测试、文档与任务状态、开发流程、依赖与安全。基线是 main `e1509d3`（S-09c 合并之后）。
严重程度：**P1** 会让问题漏过去或误导决策；**P2** 拖慢开发或增加维护成本；**P3** 小问题。

## 结论

没有发现影响资金安全或交易正确性的问题，风控、订单执行和受保护路径都没有改动。
主要问题是**检查有盲区**（CI 不跑前端）和**文档与实际脱节**（任务索引缺 50 多项、十几份任务单的状态过期）。
这两类都已修正，并加了测试防止再犯。其余是提案，需要协调方或 Dylan 决定。

## 发现与处理

| # | 发现 | 级别 | 处理 |
|---|---|---|---|
| 1 | **CI 完全不跑前端**：没有 tsc、正式构建和 Playwright。网页改坏了，CI 照样是绿的，只靠开发者本机跑 | P1 | 已修：`ci.yml` 新增前端任务（ubuntu + 自带 Chrome），跑类型检查、正式构建和 172 个端到端用例。视觉回归 4 个用例的基线是 Windows 截的，Linux 字体渲染不同，CI 里不跑，本机照常跑 |
| 2 | **任务索引缺项**：`docs/tasks/README.md` 只列了约 30 个任务，实际有 81 份任务单 | P1 | 已修：按各任务单重写索引（82 行，含 B-05x、T-10a–f 子任务）。新增 `tests/arch/test_task_index.py`：每份任务单都必须登记，状态必须与任务单一致 |
| 3 | **任务单状态过期**：B-05w、S-09（S-09c）、UI-02、T-09a、R-06a 已合并却还写着“待评审”；D-02、D-04、H-01、S-05a、S-12、S-13、S-01a-arch、S-01a-confirm、S-01c-arch 写着 IN REVIEW 但没有合并提交号 | P1 | 已修：15 份任务单补上合并提交号，状态仍是“待逐项验收”（合并不等于验收） |
| 4 | **G4 清单过期**：R-03、R-05、R-07、R-08 写着“待合并”，其实早已合并；没有提到 B-05w 的残留风险和 B-05x | P1 | 已修：改为“已合并、待逐项验收”，并加一段接线说明：B-05w 不是 IBKR 联调证据，接入真实券商前必须先处理残留风险表、完成 B-05x |
| 5 | **定时炸弹没有定期发现机制**：S-01f、B-05 各出过一次（测试写死日期，过了那天就失败），都是碰巧在别的 PR 里暴露 | P1 | 已修：`ci.yml` 加每周二定时全量运行，没有新提交时也能在一周内发现。另外排查了测试里所有读真实时钟的地方，都是相对时间，没有新的写死日期 |
| 6 | **测试继承 git 环境变量会弄坏共享仓库配置**（11-06 事故：`core.bare=true`） | P1 | #76 已修。这次复查了测试里其余的 git 调用：`test_tracked_layout`、`test_fonts` 只在仓库目录里只读执行，继承的变量指向的正是这个仓库，没有问题 |
| 7 | CLAUDE.md 目录地图缺装配层 `server/app`、平台层 `server/platforms`、`tests/contract`、`tests/support`；依赖规则漏写 app 可以直接 import broker、不得直接 import oms | P2 | 已修，与 `pyproject.toml` 的 import-linter 契约一致 |
| 8 | Windows CI 也统计覆盖率：覆盖率只是报告、没有门槛，插桩在 Windows 上开销最大 | P2 | 已修：覆盖率只在 ubuntu 上统计 |
| 9 | **测试之间互相 import 辅助代码**：30 处，`tests/integration/risk/test_core.py`、`test_limit_rules_engine.py`、`tests/integration/app/test_tradingd.py`、`tests/unit/api/test_app.py` 是被引用最多的几个。改一个测试文件可能让别处的测试失败，也不容易看出哪些是共用的 | P2 | 提案：移到 `tests/support/`。这是纯移动，但要动约 20 个文件，评审量大。建议拆成几个小 PR，每次移一组，并用测试名清单比对确认用例一个没少；也可以在以后改到这些文件时顺手移 |
| 10 | **已合并的旧分支堆积**：本地约 65 个、远端约 60 个任务分支都已压缩合并，另有 `whliang1986-glitch-patch-1` | P3 | 提案：删除远端已合并分支。删除不可撤销，又是对外操作，**等协调方或 Dylan 确认**；我先列清单，不动手 |
| 11 | **依赖更新没人处理**：dependabot 开了 14 个更新分支（11-04、11-05），其中有大版本升级：actions/checkout、setup-node、setup-uv 到 v7，duckdb 到 2.0 | P2 | 提案：小版本和补丁版本合成一个 PR，跑全量检查后合并；大版本逐个评估（duckdb 2.0 影响数据湖，单独做）。是否开始由协调方排期 |
| 12 | **主检出目录里有不属于任何任务的文件**（`wiring-fakebroker` 分支） | P3 | 只列出，没有删，见下方清单 |
| 13 | **编辑后钩子的 `ruff --fix` 会删掉“还没用上”的 import**：分几步编辑时，先加的 import 会被当成未使用删掉，要补回来。这次会话里出现了七八次 | P2 | 提案：钩子里关掉 F401 的自动修复（`--unfixable F401`），只报告、不删除。`.claude/` 是受保护路径，只能 Dylan 改 |
| 14 | 本机没有 `gh` 命令行，PR 只能交给协调方开 | P3 | 现状可用。Dylan 若希望开发会话自己开 PR，可以装 GitHub CLI 并登录 |
| 15 | **合并不等于验收的积压**：已合并、待逐项验收的任务有 30 多个，其中 G4 要求的 P0 任务不少；任务单状态的写法也不统一（IN REVIEW、IMPLEMENTED 后面各自带说明） | P2 | 提案：G4 前安排一轮逐项验收（G4 清单第 1 节已有表）。状态写法这次没有统一，以免改动任务单正文 |
| 16 | 指定开发分支在压缩合并后复用：红线不许强推，只能把 main 合进来，分支历史里会带旧提交 | P3 | 不影响结果（PR 压缩合并，差异只有新改动）。记录在这里，以后照此处理 |

## 检查结果（没有问题的部分）

- **结构**：目录与 V4.1 第 4 章一致；`server/ai`、`server/backtest` 只有占位文件，属于计划内（M2、M3）。
- **分层与唯一下单出口**：import-linter 10 条契约全部保持；新增的 `server/app/wiring.py`、`diagnostics.py` 在装配层，受“其他层不得依赖 app”“app 不得直接依赖 oms”两条契约约束。arch_scan 这次还拦下了日终报告里的受保护名（`reconciliation`），说明扫描对新文件有效。
- **死代码**：vulture、knip 都没有发现。
- **依赖漏洞**：pip-audit 没有已知漏洞（281 个依赖）；npm 生产依赖没有漏洞。
- **许可证**：281 个依赖，禁止 0、需确认 0。
- **密钥**：gitleaks 在 pre-commit（暂存区）和 CI（全历史）都在跑，CI 还有“能拦住测试密钥”的自检。
- **pre-commit 与 CI**：pre-commit 跑 ruff、mypy、import-linter、arch_scan、架构测试和 gitleaks；全量 pytest 只在 CI。这个分工合理（全量 14 分钟，不适合每次提交）。

## 全量测试与耗时

本机（Windows）在自检分支上实测：**3438 通过、2 跳过，852 秒**。两个跳过都是“这台机器没有建符号链接的权限”（Windows 要开发者模式），属于平台条件，测试本身照常存在。网页端 Playwright 全量 110 通过，66 个设计截图用例按设计只在设置 `SHOTS` 时运行。

最慢的 7 个用例合计约 290 秒，占全量的三分之一，都是随机序列或属性测试：

| 秒 | 用例 |
|---|---|
| 56 | `oms/test_order_manager.py::test_duplicated_and_shuffled_reports_book_each_fill_once` |
| 52 | `oms/test_order_manager.py::test_random_cancel_amend_fill_sequences_stay_consistent` |
| 51 | `oms/test_order_manager.py::test_random_race_sequences_with_delayed_reports_stay_consistent` |
| 39 | `oms/test_order_manager.py::test_corrections_duplicated_and_shuffled_end_with_final_values` |
| 34 | `data/test_jobs.py::test_backfill_writes_every_shard_with_a_quality_report` |
| 33 | `fault/test_oms_faults.py::test_random_fault_sequences_keep_oms_invariants` |
| 27 | `app/test_strategies.py::test_benchmark_outside_its_window_and_broken_inputs` |

提速提案（**没有做**，需要讨论）：
- 订单相关的随机测试是 O-04 验收的核心证据（重复成交、竞态），**不应该在 PR 里减少样本量**。可选做法是 PR 保持现状，另在每周定时运行里加大样本量，抓更罕见的序列。
- 并行执行（pytest-xdist）理论上能快 2–4 倍，但进程间通道、单实例锁、端口相关的测试需要逐个确认能并行，风险不小，建议单独立项。
- 回补测试（34 秒）和策略窗口测试（27 秒）值得看看有没有不必要的重复构造，可以顺手优化，不影响断言。

## 主检出目录里的文件（未删除）

`C:\Users\whlia\Desktop\量化自动交易系统`，分支 `wiring-fakebroker`（落后 main 3 个提交）：

| 文件 | 说明 | 建议 |
|---|---|---|
| `server/app/wiring.py`、`server/app/__main__.py`（修改）、`tests/integration/app/test_wiring.py`、`docs/tasks/B-05w.md` | B-05w 评审之前的版本；评审后的版本已经合并到 main（7ee2aa7），两者内容不同，以 main 为准 | 可以丢弃 |
| `docs/design/screenshots/console-demo-intel-2560x1440.png` | 11-05 的界面 DEMO 截图，约 580 KB，不在仓库里 | Dylan 决定是否保留；要入库的话超过 pre-commit 的 500 KB 上限 |
| `docs/runbooks/settings.local.json` | 一份本地权限白名单草稿（允许哪些命令免确认），11-04 | 不应入库（是本机设置）；Dylan 决定保留还是删除 |

## 需要协调方或 Dylan 决定的事

1. 是否删除远端已合并的任务分支（第 10 项）。
2. 依赖更新的排期（第 11 项）。
3. 编辑后钩子关掉 F401 自动修复（第 13 项，`.claude/` 只能 Dylan 改）。
4. 主检出目录里那三类文件怎么处理（第 12 项）。
5. 测试辅助代码迁到 `tests/support/` 是现在做还是顺手做（第 9 项）。
