按代码事实看，这次重构方向是对的，关键改进已经落地：你确实引入了结构化结果 ExecutionResult、去掉了全局 \_initialized、把 working_dir 真正接到了 Stata cd，CLI 也能输出 JSON 和非零退出码。infra/stata_engine.py (line 42) infra/stata_engine.py (line 190) main.py (line 70)

但从审查角度，当前实现还有 6 个值得优先处理的问题：

严重: timeout 仍然是“事后标记”，不是“执行监督”。代码只在 Stata 跑完以后比较 elapsed_ms，无法真正中止卡死任务，这和 Agent 闭环里“有界执行”不是一回事。infra/stata_engine.py (line 179) infra/stata_engine.py (line 201)
严重: 相对路径契约仍然不一致。working_dir 已经用于 Stata 会话，但 run_script、load_data、export_data 仍然先对传入路径做 Path(...).resolve()，这会把相对路径锚定到 Python 当前目录，而不是 job 的 working_dir。infra/stata_engine.py (line 128) infra/stata_engine.py (line 135) infra/stata_engine.py (line 150)
高: run(command) 对“命令块”没有稳定语义。你包装的是 capture noisily {command}，这对多行 block 的错误传播和 \_rc 采样并不可靠；对 .do 文件问题较小，但对后续 Agent 调试型 inline block 是隐患。infra/stata_engine.py (line 124) infra/stata_engine.py (line 193)
高: 错误传输模型仍然分裂。运行失败返回 ExecutionResult，初始化失败却抛异常；CLI 又把所有初始化失败都折叠成 engine_init_error，丢掉了你已经定义好的 license_error / not-installed 语义。infra/stata_engine.py (line 73) main.py (line 77)
中: summary 仍然不够“帮助 Agent 快速命中修复”。当前摘要逻辑经常退化成 r(199); 这种返回码回显，而不是那句真正有信息量的报错文本。infra/stata_engine.py (line 300)
中: artifacts 字段现在始终为空，说明执行器还没有真正承担“结果发现”职责。对 Agent 来说，这会迫使上层再做目录扫描，协议边界仍然偏薄。infra/stata_engine.py (line 210) infra/stata_engine.py (line 228)
补充校验结论：静态层面 python -m py_compile 已通过；仓库里没发现 tests/ 或等价测试文件，所以这轮重构还没有被行为测试锁住。另一个环境侧事实是，本机这里的 uv.exe 启动仍有系统关联问题，所以我没把 uv run 作为可靠验证依据。

深度交互

从第一性原理看，你现在优化的对象仍然有一点偏。你的真实目标不是“让 Agent 更会读 Stata 日志”，而是“让底层执行器把一次执行收敛成可判定、可复现、可终止的事实”。只要这一层还建立在进程内交互式 pystata 会话上，timeout、隔离、状态污染、产物发现这些核心能力就会一直被动。

更直接地说：如果你真接受破坏性变更，下一步不该继续打磨 StataEngine，而应该把它升级成 StataJobRunner。pystata 可以保留，但应降级为一个可选 backend，而不是默认监督运行时。默认执行路径应该是“独立 job + 独立工作目录 + 可杀死的子进程 + 结构化 manifest”，因为这才匹配未来 Skill/MCP 的标准化调用边界。

# Stata 执行基础设施 V2

## Summary

- 将当前 `StataEngine` 从“进程内交互执行器”重构为“作业型执行器”。
- 默认 backend 改为可监督的子进程 Stata 调用；`pystata` 保留为兼容/调试 backend，不再承担生产级监督职责。
- 协议目标从“返回日志”升级为“返回 job manifest”。

## Key Changes

- 公共接口改为 `run_do(script_path, spec)` 和 `run_inline(commands, spec)`；废弃当前对外暴露的自由字符串 `run(command)` 语义。`run_inline` 一律先物化成临时 `.do` 文件再执行。
- 用 `JobSpec` 明确输入：`working_dir`、`timeout_sec`、`artifact_globs`、`env_overrides`。相对路径一律相对于 `working_dir` 解析。
- 用 `JobResult` 替代当前 `ExecutionResult`，固定字段为：`status`、`phase`、`exit_code`、`error_kind`、`summary`、`job_dir`、`log_path`、`log_tail`、`artifacts`、`elapsed_ms`、`backend`、`working_dir`。
- 每次执行创建独立 `job_dir`，保存输入脚本副本、原始日志、结构化 `result.json`、产物清单，避免会话残留和“最后一次输出”式状态共享。
- 初始化失败也走统一结果协议，不再让上层同时处理异常和结果对象；异常只保留给真正的编程错误。
- 错误分类按执行阶段区分，至少包含：`bootstrap_error`、`input_error`、`timeout`、`stata_parse_or_command_error`、`stata_runtime_error`、`artifact_collection_error`。

## Test Plan

- 相对路径脚本、数据文件、输出文件都基于 `working_dir` 正确解析。
- 未知命令和语法错误返回结构化失败，摘要包含首条有信息量的报错文本，而不是仅返回 `r(code)`。
- 长时间脚本会被真实终止并返回 `timeout`，不会阻塞后续 job。
- 同一进程连续执行多个 job 时，工作目录、日志、产物、返回码互不污染。
- 初始化失败、缺失脚本、缺失 license、正常回归、生成结果文件这 5 类场景都有固定断言。

## Assumptions

- 接受破坏性 API 变更，并优先保证监督能力和协议清晰度，而不是兼容当前 `run(command)` 调用方式。
- 当前主要部署环境是 Windows + Stata 17，本地单机执行优先，并发可以后置。
- “结果是否符合经济学预期”继续放在上层分析器，不进入 `infra`。
