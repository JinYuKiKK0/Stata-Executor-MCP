# StataAgent 执行基础设施

该模块现在只保留一种执行方式：子进程调用本机 Stata。

设计原则：

- 每次执行都是独立 job
- `JobResult` 只描述执行事实，不判断经济学结果是否“合理”
- `run.log` 是主执行日志
- 外层 batch/process 日志只要与 `run.log` 重叠，就直接删除；只有存在独立信息时才收敛进当前 `job_dir`

## 公开接口

`infra` 当前导出：

- `StataConfig`
- `JobSpec`
- `JobResult`
- `StataJobRunner`

内部模块职责：

- `infra/config.py`: 静态配置与目录解析
- `infra/models.py`: 执行协议模型，定义 `JobSpec` / `JobResult`
- `infra/executable_resolver.py`: Stata 可执行文件解析与命令构造
- `infra/stata_engine.py`: job 编排、日志收敛、产物收集、结果落盘

### `StataConfig`

静态配置，定义 Stata 路径、工作目录和 job 根目录。

关键字段：

- `edition`
- `stata_path`
- `working_dir`
- `job_root`
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
- `run_log_path`
- `process_log_path`
- `log_tail`
- `diagnostic_excerpt`
- `error_signature`
- `failed_command`
- `artifacts`
- `elapsed_ms`
- `working_dir`

`JobResult` 只回答这些问题：

- do 文件有没有完整跑完
- 退出码是多少
- 报错发生在输入、启动、执行还是产物收集阶段
- 主日志和外层进程日志分别在哪里
- 即便执行失败，过程中已经生成了哪些产物
- 哪段机械诊断摘录最值得先看
- 最近一条命令和首条高信号错误是什么

## Python 调用

```python
from pathlib import Path

from infra import JobSpec, StataConfig, StataJobRunner

runner = StataJobRunner(
    StataConfig(
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
print(result.run_log_path)
print(result.process_log_path)
print(result.log_tail)
print(result.diagnostic_excerpt)
print(result.error_signature)
print(result.failed_command)
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
python main.py run-do ./path/to/analysis.do ^
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
python main.py run-do D:/orders/Archive/stata/tech_pregnant_1/scripts/analysis.do --stata-path "D:/Program Files/Stata17/StataMP-64.exe" --json
```

失败时 CLI 会返回非零退出码；标准输出始终是 `JobResult` JSON。
即便是 CLI 参数错误，也会返回稳定 JSON 和退出码 `2`，而不是 Python traceback。

## Job 目录结构

每次执行都会在 `job_root` 下创建一个独立目录，至少包含：

- `input.do`
- `wrapper.do`
- `run.log`
- `result.json`

另外：

- 如果 Stata 额外生成了外层 batch/process 日志，runner 会先尝试和 `run.log` 去重
- 如果 `run.log` 已被完整包含在外层日志里，这份外层日志会被直接删除
- 只有无法安全删掉且仍包含独立信息时，这份日志才会被保存为 `process.log`
- `result.json` 会显式区分 `run_log_path` 和 `process_log_path`

## 可执行文件解析

当 `stata_path` 指向安装目录或某个 Stata 可执行文件时，runner 会优先在同目录里寻找更像 batch/headless 入口的可执行文件，再回退到普通 GUI 可执行文件。

这意味着：

- 如果本机只有 `StataMP-64.exe`，runner 仍会使用它
- 如果将来安装目录里同时存在 console/batch 入口，runner 会优先选它们

`stata_path` 是强制配置项。未显式提供时，runner 会直接返回 `bootstrap_error`，不会读取环境变量、Windows 注册表或常见安装目录进行自动发现。

## 测试

当前仓库包含基于 fake Stata 可执行文件的行为测试，覆盖：

- 缺失脚本
- bootstrap 失败
- 相对路径解析
- 语法/命令错误摘要
- timeout
- job 隔离
- 外层 process log 收敛进 `job_dir`
- 失败作业产物收集
- CLI 参数错误 JSON 协议

运行方式：

```bash
python -m unittest discover -s tests -v
```

## Codex Skill

仓库内置了一个可复用 skill 源目录：

- `skills/stata-job-runner/`

它把当前执行基础设施封装成给其他 Agent 调用的稳定入口：

- `SKILL.md`: skill 工作流与使用边界
- `scripts/run_stata_job.py`: 优先走 `uv run python main.py ... --json`，必要时降级到 repo `.venv` Python 的包装脚本
- `references/contract.md`: 输入、输出、日志、依赖与路径解析约定
- `references/mcp-contract.md`: 未来 MCP 工具面与结果协议的收敛目标

这个 skill 现在是过渡性的调用层，不是最终产品边界；长期公共边界应迁移为 MCP。
