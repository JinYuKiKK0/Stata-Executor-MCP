# Stata-Executor-MCP 待修问题清单

本文件记录 2026-04-24 代码审查发现的全部问题，按严重度 H/M/L 三级分组。修一项勾一项（`[x]`），注明所在 commit 或 PR。

**修复建议顺序**：先规范合规 + 死代码（H3/H4/H6）→ 其余 H 系列 → M 系列 → L 系列。

---

## 🔴 高优先级（6 项）

### - [x] H1. `render_result_text` 表格识别过窄，且会覆盖 full text

- **文件**：`stata_executor/engine/output_parser.py`
- **行号**：52-84（`render_result_text`）、87-129（`extract_empirical_result_blocks`）
- **描述**：
  - 抽取器只识别两种格式（`Linear regression` 同行含 `Number of obs`；`Variable | Obs` 开头的 `summarize`）。
  - `logit` / `probit` / `xtreg` / `reghdfe` / `ivreg2` / `areg` / `tabulate` / `test` / `margins` / `etable` 等**都不会**被识别。
  - 一旦 `blocks` 非空，第 82-84 行 `return "\n\n".join(blocks)` **直接替换** filtered 全文，未命中的表格彻底丢失。
- **影响**：违背 README 承诺的"面向模型直接消费的干净实证结果正文"。Agent 拿到的 `result_text` 可能缺关键实证信息。
- **候选修法**：
  - **A（推荐）**：删除 `extract_empirical_result_blocks`，只返回 filtered 全文。
  - **B**：blocks 作为 **补充** 而非 **替代**，附在 full text 末尾。
  - **C**：扩展识别更多表格（维护成本高，不推荐）。
- **决策**：待定

---

### - [x] H2. `_finalize_process_log` 子串去重存在数据丢失风险

- **文件**：`stata_executor/engine/process_runner.py`
- **行号**：75-96，核心在 82 行
- **描述**：`if normalized_run and normalized_run in normalized_raw` 用 `in` 子串匹配。若 `run.log` 恰为 `wrapper.log` 前缀（Stata 被强杀、run.log 半写），代码会删掉信息更完整的 wrapper.log。
- **影响**：超时或异常退出时诊断信息可能丢失。
- **候选修法**：
  - 简洁方案：始终保留两份 log，不去重。
  - 或改为严格相等 `normalized_run == normalized_raw` 才去重。
- **决策**：待定

---

### - [x] H3. MCP `_initialized` 是死代码，违反 MCP 握手规范

- **文件**：`stata_executor/adapters/mcp.py`
- **行号**：23（写入 False）、49（写入 True）；**全文无读取**
- **描述**：
  - 按 MCP 规范，server 在收到 `notifications/initialized` 之前应只响应 `initialize` / `ping`，其余请求应返回错误。
  - 当前实现允许客户端跳过握手直接调 `tools/call`，`_initialized` 字段从未参与判断。
- **影响**：规范合规问题；功能上主流客户端（Claude Code 等）都会正确握手，实际不会触发。
- **修法**：在 `_handle_message` 方法开头加守卫：
  ```python
  if method not in ("initialize", "ping") and not self._initialized:
      if request_id is not None:
          self._write_error(request_id, -32002, "Server not initialized.")
      return
  ```
- **分类**：🏷️ 规范合规 + 死代码
- **决策**：待定

---

### - [x] H4. `_coerce_text` 是死代码（过度防御）

- **文件**：`stata_executor/engine/process_runner.py`
- **行号**：36（调用处）、104-111（函数定义）
- **描述**：`subprocess.run(..., capture_output=True, text=True)` 与 `TimeoutExpired.stdout/stderr` 在 `text=True` 下必为 `str | None`，函数里的 `bytes`/`bytearray`/`memoryview` 分支永不触发。
- **影响**：纯过度防御，违反 KISS。
- **修法**：删除 `_coerce_text`；`_compose_process_output` 已能处理 `None`，直接传 `exc.stdout` / `exc.stderr` 即可。
- **分类**：🏷️ 死代码
- **决策**：待定

---

### - [x] H5. artifact 收集失败把 succeeded 翻成 failed

- **文件**：`stata_executor/engine/executor.py`
- **行号**：110-131
- **描述**：Stata 脚本 exit 0 正常完成，只要 artifact 扫描遇任何 `OSError`（杀毒软件锁文件、临时路径权限等），整体就被翻转为 `status="failed"` + `error_kind="artifact_collection_error"`。
- **影响**：对 Agent 是致命误导——Agent 会去"修正"根本不存在的 Stata 错误。
- **修法**：Stata 本身成功时，artifact 失败降级为 summary 附言；Stata 本身失败时维持合并逻辑：
  ```python
  except OSError as exc:
      if result.status == "succeeded":
          return self._persist_result(runtime, replace(
              result,
              summary=result.summary + f" (artifact collection partially failed: {exc})",
              artifacts=[],
          ))
      # stata 失败 → 走原合并逻辑
  ```
- **决策**：待定

---

### - [x] H6. MCP 协议版本缺 `2024-11-05`

- **文件**：`stata_executor/adapters/mcp.py`
- **行号**：12
- **描述**：`SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")`，漏掉 MCP 初版日期 `2024-11-05`。老版本 Claude Desktop、部分 MCP Inspector、老 SDK 仍发此版本号。
- **影响**：老客户端连接会被拒。
- **修法**：
  ```python
  SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
  ```
- **分类**：🏷️ 规范合规
- **决策**：待定

---

## 🟡 中优先级（6 项）

### - [ ] M1. wrapper.do 未转义路径中的引号

- **文件**：`stata_executor/engine/preparation.py`
- **行号**：40-46
- **描述**：`f'log using "{runtime.run_log_path.as_posix()}", ...'` 若路径含 `"` 字符，生成的 Stata 代码语法错误。Windows 路径不含引号，Unix 也罕见，属 latent bug。
- **修法**：路径转义，或在 `_resolve_working_dir` 中拒绝含引号路径并返回 `input_error`。
- **决策**：待定

---

### - [ ] M2. `version 17.0` 硬编码

- **文件**：`stata_executor/engine/preparation.py`
- **行号**：36
- **描述**：绑死 Stata 17+ 语法族，Stata 15/16 用户会因 `version` 命令拒绝较新语法而失败。
- **修法**：由 env / config 可覆盖，默认保持 17.0。
- **决策**：待定

---

### - [ ] M3. `_string_or_none` 接受空字符串，与 None 语义混淆

- **文件**：`stata_executor/adapters/mcp.py`
- **行号**：300-305
- **描述**：客户端传 `"working_dir": ""` 会通过校验，`Path("").resolve()` 解释为 CWD，和 `None`（也走 CWD fallback）行为相同但语义不清。
- **修法**：`value.strip() == ""` 时归一为 `None` 或抛 `ValueError`。
- **决策**：待定

---

### - [ ] M4. `artifact_globs` 只挡绝对路径，不挡 `..` 穿越

- **文件**：`stata_executor/engine/preparation.py`
- **行号**：12
- **描述**：`../secret/**/*` 仍可跳出 `working_dir`，与"artifacts 相对 working_dir"承诺不一致。
- **修法**：增加 `".." in Path(pattern).parts` 检查。
- **决策**：待定

---

### - [ ] M5. CLI 与 MCP 的 Request 构造代码重复

- **文件**：`stata_executor/adapters/cli.py:62-87`、`adapters/mcp.py:107-131`
- **描述**：两处独立构造 `RunDoRequest` / `RunInlineRequest`，新增字段要双改，易漏同步。
- **修法**：抽出 `build_run_do_request(...) / build_run_inline_request(...)` 小函数，放到 `adapters/__init__.py` 或 `adapters/requests.py`。不引入 Pydantic 等额外抽象。
- **决策**：待定

---

### - [ ] M6. 测试覆盖缺口

- **文件**：`tests/test_stata_executor.py` / `tests/test_engine_modules.py`
- **未覆盖路径**：
  - `subprocess.run` 抛 `OSError`（executable 不可执行）→ `process_runner.py:48-58`
  - `collect_artifacts` 抛 `OSError` → `executor.py:110-131`
  - MCP 未 initialize 就调 `tools/call`
  - 含中文/空格的 `working_dir` 与 `script_path`
  - `run.log` 缺失场景
  - `logit` / `xtreg` / `tabulate` 输出完整性（配 H1 修复）
- **修法**：各补 1 个用例，延续既有 fake Stata 模式。
- **决策**：待定

---

## 🟢 低优先级（6 项）

### - [ ] L1. `_execute_prepared_job` 嵌套分支略深

- **文件**：`stata_executor/engine/executor.py:105-144`
- **描述**：`try/except OSError` 与 `result.status == "failed"` 分支嵌套较深，可小幅扁平化。当前仍可读。
- **决策**：待定

---

### - [ ] L2. `extract_last_command_block` 的命令行识别可能误判

- **文件**：`stata_executor/engine/output_parser.py:162`
- **描述**：用 `raw_line.startswith(". ")` 认定命令行起点，`display ". something"` 的输出会被误判为命令。低概率。
- **决策**：待定

---

### - [ ] L3. `errors="ignore"` 静默吞非 UTF-8 字节

- **文件**：`stata_executor/engine/process_runner.py:110, 121`
- **描述**：`read_text(errors="ignore")` 丢弃非 UTF-8 字节，排查困难。改 `errors="replace"` 保留占位符更利排查。
- **决策**：待定

---

### - [ ] L4. `SubprocessOutcome` 三个 text 字段缺 docstring

- **文件**：`stata_executor/engine/process_runner.py:11-19`
- **描述**：`process_output` / `process_text` / `primary_text` 三字段语义需从调用点反推，应加一行注释说明。
- **决策**：待定

---

### - [ ] L5. `_env_error` 在 doctor 调用路径也生效

- **文件**：`stata_executor/adapters/mcp.py:94-96, 99-104`
- **描述**：doctor 本是用来诊断配置的，当前遇到 env 错误会直接返回协议级错误，无法进入结构化 DoctorResult。应允许 doctor 在 env 不合法时仍返回带 errors 字段的 DoctorResult。
- **决策**：待定

---

### - [ ] L6. MCP server 缺 stderr debug 日志通道

- **文件**：`stata_executor/adapters/mcp.py`
- **描述**：无 debug 输出，排查协议问题时不便。可加 `STATA_EXECUTOR_DEBUG` 环境变量控制的 stderr 日志。
- **决策**：待定

---

## 修复进度

| 批次 | 范围 | 状态 |
|---|---|---|
| 第 1 批 | 规范合规 + 死代码（H3 / H4 / H6） | ✅ 已完成（commit 582410c） |
| 第 2 批 | 其余 H 系列（H1 / H2 / H5） | ✅ 已完成 |
| 第 3 批 | M 系列（M1 - M6） | ⏳ 待开始 |
| 第 4 批 | L 系列（L1 - L6） | ⏳ 待开始 |
