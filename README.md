# StataAgent 执行基础设施 V2

该模块现在是作业型执行器，而不是进程内交互式 `StataEngine`。

核心目标：

- 每次执行都是独立 job
- 默认使用可监督的子进程 Stata backend
- 对上层返回结构化 manifest，而不是原始日志字符串

## 公开接口

`infra` 当前导出：

- `StataConfig`
- `JobSpec`
- `JobResult`
- `StataJobRunner`

### `StataConfig`

静态配置，定义默认 backend、Stata 路径、工作目录和 job 根目录。

关键字段：

- `backend`: `subprocess` 或 `pystata`
- `stata_path`: Stata 可执行文件路径，或安装目录
- `working_dir`: 相对输入输出的基准目录
- `job_root`: 每次 job 的独立落盘目录
- `default_timeout_sec`
- `artifact_globs`
- `env_overrides`

### `JobSpec`

单次执行覆盖项：

- `working_dir`
- `timeout_sec`
- `artifact_globs`
- `env_overrides`

### `JobResult`

统一返回协议：

- `status`
- `phase`
- `exit_code`
- `error_kind`
- `summary`
- `job_dir`
- `log_path`
- `log_tail`
- `artifacts`
- `elapsed_ms`
- `backend`
- `working_dir`

## Python 调用

```python
from pathlib import Path

from infra import JobSpec, StataConfig, StataJobRunner

runner = StataJobRunner(
    StataConfig(
        backend="subprocess",
        edition="mp",
        stata_path="D:/Program Files/Stata17/StataMP-64.exe",
        working_dir=Path.cwd(),
        job_root=Path("logs/jobs"),
    )
)

result = runner.run_do(
    "analysis.do",
    JobSpec(
        timeout_sec=120,
        artifact_globs=("output/**/*.rtf", "output/**/*.xlsx"),
    ),
)

print(result.status, result.exit_code, result.error_kind)
print(result.summary)
print(result.job_dir)
print(result.log_tail)
```

运行 inline 命令时，runner 会先物化成 `input.do`，再以隔离 job 执行：

```python
result = runner.run_inline(
    """
    sysuse auto, clear
    regress price weight mpg
    """.strip(),
    JobSpec(timeout_sec=60),
)
```

## CLI

### 执行 do 文件

```bash
python main.py run-do D:/orders/Archive/stata/tech_pregnant_2zi/scripts/analysis.do ^
  --backend subprocess ^
  --stata-path "D:/Program Files/Stata17/StataMP-64.exe" ^
  --working-dir . ^
  --artifact-glob "output/**/*.rtf"
```

### 执行 inline 命令

```bash
python main.py run-inline "sysuse auto, clear" --stata-path "D:/Program Files/Stata17/StataMP-64.exe"
```

### 机器调用

```bash
python main.py run-do D:/orders/Archive/stata/tech_pregnant_2zi/scripts/analysis.do --stata-path "D:/Program Files/Stata17/StataMP-64.exe" --json
```

失败时 CLI 会返回非零退出码；标准输出始终是 `JobResult` JSON。

## Job 目录结构

每次执行都会在 `job_root` 下创建一个独立目录，至少包含：

- `input.do`
- `wrapper.do`
- `run.log`
- `result.json`

这样上层 Agent 可以直接基于 manifest 做错误修复，不必再依赖“最后一次日志”这类共享状态。

## Backend 说明

### `subprocess`

默认 backend。

优点：

- 有真实超时终止能力
- job 级隔离更清晰
- 更适合作为 Skill/MCP 底座

### `pystata`

仅保留为兼容/调试 backend。

限制：

- 无法提供真正的抢占式 timeout
- 更容易受进程内状态影响

## 测试

当前仓库包含基于 fake Stata 可执行文件的行为测试，覆盖：

- 缺失脚本
- bootstrap 失败
- 相对路径解析
- 语法/命令错误摘要
- timeout
- job 隔离

运行方式：

```bash
python -m unittest discover -s tests -v
```

