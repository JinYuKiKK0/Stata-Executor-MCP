# Stata 执行器 (Stata Executor)

`stata_executor` 是一个面向 Agent 的本地 Stata 执行 MCP server，让 Agent 具备运行 do 脚本/内联命令并读取稳定执行结果的能力。

它返回两类信息：一类是稳定的执行事实，另一类是可直接给模型消费的干净实证结果正文。它不负责解释经济学或实证结果。

## 安装

```bash
uv sync
```

## 配置

`stata_executor` 通过 MCP 启动 JSON 的 `env` 字段注入环境变量：

- `STATA_EXECUTOR_STATA_EXECUTABLE`: Stata 可执行文件路径（必填）
- `STATA_EXECUTOR_EDITION`: 版本（可选，`mp` / `se` / `be`，默认 `mp`）

示例：

```json
{
  "mcpServers": {
    "stata-executor": {
      "command": "D:/Developments/PythonProject/Stata-Executor-MCP/.venv/Scripts/python.exe",
      "args": ["-m", "stata_executor"],
      "cwd": "D:/Developments/PythonProject/Stata-Executor-MCP",
      "env": {
        "STATA_EXECUTOR_STATA_EXECUTABLE": "D:/Program Files/Stata17/StataMP-64.exe",
        "STATA_EXECUTOR_EDITION": "mp"
      }
    }
  }
}
```

启动命令：`python -m stata_executor`。

## 暴露的工具 (Tools)

- `doctor`: 检查环境与配置
- `run_do`: 执行现有的 .do 文件
- `run_inline`: 执行单条或多条 inline 命令

## 结果结构 (Result Shape)

`run_do` / `run_inline` 返回的结构化内容 (`structuredContent`) 包含以下字段：

- `status`: 执行状态 (`succeeded`, `failed`)
- `error_kind`: 失败时的错误分类(`bootstrap_error` / `input_error` / `timeout` / `stata_parse_or_command_error` / `stata_runtime_error` / `artifact_collection_error`),成功时为 `null`
- `result_text`: 过滤命令回显后的完整结果正文，面向模型直接消费
- `diagnostic_excerpt`: 失败时围绕末次命令与错误行的诊断片段
- `artifacts`: 本次执行新增或变更的产物绝对路径列表

`doctor` 返回:

- `ready`: 配置与可执行文件是否就绪
- `errors`: 未就绪时的错误清单(就绪时为空列表)

执行失败时响应的 `isError=true` 且仍然返回完整 `structuredContent`，便于 Agent 基于诊断字段进行恢复。

## 测试

```bash
python -m unittest discover -s tests -v
```
