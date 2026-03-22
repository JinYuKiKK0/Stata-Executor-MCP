# Stata 执行器 (Stata Executor)

`stata_executor` 是一个小型、独立的 Stata 执行能力模块，面向 Agent、IDE 和本地自动化脚本设计。

它通过三种形式暴露统一的稳定边界：

- **MCP (stdio)**: 面向智能体集成
- **CLI**: 面向命令行调试和运维
- **Python API**: 面向编程复用

该工具仅负责报告执行事实：状态、阶段、退出码、日志、产物路径以及诊断摘要。它不负责解释经济学或实证结果。

## 安装

```bash
uv sync
```

## 配置

Stata 路径解析仅支持用户级配置文件或单次调用的显式覆盖。

默认配置路径：

- **Windows**: `%APPDATA%/stata-executor/config.json`
- **macOS**: `~/Library/Application Support/stata-executor/config.json`
- **Linux**: `${XDG_CONFIG_HOME:-~/.config}/stata-executor/config.json`

配置示例：

```json
{
  "stata_executable": "D:/Program Files/Stata17/StataMP-64.exe",
  "edition": "mp",
  "defaults": {
    "timeout_sec": 120,
    "artifact_globs": []
  }
}
```

参考 [`examples/config.example.json`](examples/config.example.json)。

## 命令行接口 (CLI)

```bash
python -m stata_executor doctor
python -m stata_executor run-do D:/work/project/analysis.do --working-dir D:/work/project
python -m stata_executor run-inline "sysuse auto, clear\nregress price weight mpg" --working-dir D:/work/project
```

常用参数：

- `--stata-executable`: 显式指定 Stata 可执行文件路径
- `--edition`: Stata 版本 (`mp`, `se`, `be`)
- `--working-dir`: 工作目录
- `--timeout-sec`: 超时时间（秒）
- `--artifact-glob`: 产物匹配规则
- `--env KEY=VALUE`: 环境变量覆盖
- `--pretty`: JSON 格式化输出

## Agent 集成 (MCP)

```bash
python -m stata_executor.adapters.mcp
```

暴露的工具 (Tools)：

- `doctor`: 检查环境与配置
- `run_do`: 执行现有的 .do 文件
- `run_inline`: 执行单条或多条 inline 命令

## Python API

```python
from stata_executor import RunDoRequest, StataExecutor

executor = StataExecutor()
result = executor.run_do(
    RunDoRequest(
        script_path="analysis.do",
        working_dir="D:/work/project",
    )
)
```

## 结果结构 (Result Shape)

`ExecutionResult` 包含以下字段：

- `status`: 执行状态 (`succeeded`, `failed`)
- `phase`: 发生的阶段
- `exit_code`: 退出码
- `error_kind`: 错误分类
- `summary`: 执行摘要
- `job_id`: 任务唯一标识
- `job_dir`: 任务独立目录
- `working_dir`: 工作目录
- `run_log_path`: Stata 运行日志路径
- `process_log_path`: 进程日志路径
- `diagnostic_excerpt`: 关键诊断摘要
- `error_signature`: 错误特征码 (如 r(198))
- `failed_command`: 导致失败的命令
- `log_tail`: 日志尾部窗口
- `artifacts`: 生成的产物列表
- `elapsed_ms`: 耗时（毫秒）

## 测试

```bash
python -m unittest discover -s tests -v
```
