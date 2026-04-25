# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

面向 Agent 的本地 Stata 执行 MCP server。只做两件事：产出稳定的"执行事实"与清洗过的"实证结果正文"（`result_text`），**不解释**经济学或统计含义。所有对 Agent 可见的语义字段在 `stata_executor/contract/__init__.py` 中以 frozen dataclass + `Literal` 定义，改动这些字段就等于改动对外协议。

## 常用命令

- 安装依赖：`uv sync`
- 启动 MCP server（stdio）：`python -m stata_executor`
- 跑全部测试：`python -m unittest discover -s tests -v`
- 跑单个测试：`python -m unittest tests.test_stata_executor.StataExecutorTests.test_run_inline_reports_parse_error -v`
- 保留测试产物以便排查：`KEEP_TEST_ARTIFACTS=1 python -m unittest discover -s tests -v`（产物位于 `.tmp_test_runs/`）

## Lint / Format 工具链

与 csmar-mcp 对齐：ruff + pyright(strict)，统一入口 `scripts/check.py`，本地由 pre-commit framework 拉起，远端由 `.github/workflows/lint.yml` 在 push / PR 上兜底。

- 装开发依赖并激活钩子（一次性）：`uv sync --group dev && uv run pre-commit install && uv run pre-commit install --hook-type pre-push`
- 本地手动跑：`uv run python scripts/check.py`（默认 check-only；`--fix` 跑 auto-fix + format 回写）
- pre-commit 钩子跑 `--fix` 模式，pre-push 钩子跑 check-only，配置见 `.pre-commit-config.yaml`

## 配置注入

服务端**只**从环境变量读取 Stata 路径，不再解析任何用户配置文件。调试时复用仓内 `.mcp.json` 中的 env 字段即可：

- `STATA_EXECUTOR_STATA_EXECUTABLE`（必填）：可传具体可执行文件或安装目录；传目录时按 `engine/../runtime/executable_resolver.py` 的打分规则优先选 `console/batch/headless` 和 `64` 位版本。
- `STATA_EXECUTOR_EDITION`：`mp` / `se` / `be`，默认 `mp`。其他值会在调用任意工具时通过 `env_error` 直接返回失败。

## 架构（分层，严格单向依赖）

```
adapters/mcp.py      # MCP stdio 适配器：schema、参数映射、CallToolResult 包装
   └── engine/executor.py  # StataExecutor：编排 doctor/run_do/run_inline
        ├── runtime/           # 纯配置与运行目录解析（job_dir、wrapper 路径、env 合并）
        ├── engine/preparation # 校验 + 写 wrapper.do
        ├── engine/process_runner # subprocess 调用 + 日志融合
        ├── engine/output_parser  # 退出码 / 诊断 / result_text 抽取
        └── engine/artifacts      # 按 glob 做 before/after 快照式采集
contract/__init__.py # 对外数据类型（RunDoRequest / ExecutionResult / DoctorResult 等）
```

大局观（读代码前最好先懂这些）：

1. **wrapper.do 机制**（`engine/preparation.py::write_wrapper_do`）：用户的 do 文件永远被包在一个固定壳里，壳里 `capture noisily do <input>`、记 `__AGENT_RC__=_rc`、再 `exit _rc, STATA clear`。所以 `parse_exit_code` 以 `__AGENT_RC__` 为第一优先级、`r(NNN)` 为次选、subprocess returncode 兜底。改执行流程时要同步维护这套契约。
2. **每次调用起一个新 job 目录**：`<working_dir>/.stata-executor/jobs/job_<ts>_<hash>/`，下含 `input.do`、`wrapper.do`、`run.log`、`process.log`、`result.json`。`result.json` 由 `StataExecutor._persist_result` 在每条路径收尾时写盘——新增提前返回路径务必也走 `_persist_result`，否则 Agent 拿不到持久化结果。
3. **两条并行日志流**：Stata log（`run.log`，`log using ... name(agentlog)`）与 subprocess stdout/stderr（`wrapper.log`→`process.log`）。`process_runner._finalize_process_log` 只在 run.log 完全覆盖 wrapper.log 时才去重，超时或异常一律保留两份，`primary_text` 优先 run.log，否则用 process_text。
4. **`result_text` 产出策略**（`output_parser.render_result_text`）：先按 `. <cmd>` 切段，命中表格 / `r(...)` / `display` 时抽取"实证块"拼成 `result_text`；没命中就走过滤回显 + 续行 + log 样板的 fallback。**这是对外语义承诺的核心**，修改前看 `docs/FIX.md` 的 H1 条目与相关测试。
5. **可执行文件选择**在 `runtime/executable_resolver.py`：Windows 走 `/q /i /e do <wrapper>`，POSIX 走 `-b do <wrapper>`。新增操作系统支持需同时改这里和对应测试断言。
6. **Artifacts 差分**（`engine/artifacts.py`）：执行前 `snapshot_artifacts` 做 (mtime_ns, size) 快照，执行后 `collect_artifacts` 仅返回变更或新增的文件。只接受相对于 `working_dir` 的 glob，`preparation.validate_request` 会拦截绝对路径。

## 测试策略要点

- `tests/test_stata_executor.py` 不依赖真 Stata，用内联生成的 `fake_stata.py` + `fake_stata.cmd` 模拟 Stata 行为，识别 `FAKE_WRITE` / `FAKE_ERROR` / `FAKE_SLEEP` 特殊指令。改执行器或 wrapper 格式时，这个 fake 的 `parse_wrapper` 正则（`log using "..."`、`cd "..."`、`do "..."`）必须仍能匹配。
- 每个用例都在 `.tmp_test_runs/case_<hex>/` 下操作；`tearDown` 默认清理，`KEEP_TEST_ARTIFACTS=1` 时保留。
- `tests/live_test/` 下放的是真实 Stata 运行素材（`02_analysis.do` / `panel_final.csv`），只在需要连真 Stata 时用，不要在自动化测试里引用。

## 待修清单

`docs/FIX.md` 按 H/M/L 维护已识别问题（含建议修法、文件行号）。动执行器、output_parser、process_runner 之前先扫一遍这份清单，避免重踩坑；勾一项补一次说明。
