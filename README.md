# StataAgent 本地 MVP 说明（pystata）

这个 MVP 的目标是先把 Stata 在本机跑通，并给后续 Agent/MCP 封装提供稳定底座。

## 1. 每个 Python 文件负责什么

- `main.py`
  负责命令行入口。
  支持参数解析（`--script`、`--edition`、`--stata-path`），并调用 `StataEngine` 执行。
  不传 `--script` 时会执行一组 smoke test（`sysuse auto` + `summarize` + `regress`）。

- `infra/config.py`
  定义 `StataConfig` 配置数据类。
  负责管理执行参数：`edition`、`stata_path`、`working_dir`、`log_dir`。
  提供路径解析与自动建目录（`resolve_working_dir`、`resolve_log_dir`）。

- `infra/stata_engine.py`
  核心执行引擎 `StataEngine`。
  负责初始化 Stata（从 Stata 安装目录的 `utilities/` 引导 `pystata`）、执行命令、执行 do 文件、导入导出数据、写入日志。
  同时封装了统一异常：
  `StataEngineError`、`StataNotInstalledError`、`StataLicenseError`、`StataCommandError`。

- `infra/__init__.py`
  对外导出公共 API。
  让上层可以直接 `from infra import StataEngine, StataConfig` 使用，不需要关心内部文件结构。

## 2. 运行依赖与环境要求

- Python `3.12+`
- 本机已安装 Stata 17+ 且有可用 license
- 使用 UV 管理环境（项目要求）

安装依赖：

```bash
uv sync
```

## 3. 配置说明（重点）

### 3.1 Stata 安装路径

`STATA_PATH` 或 `--stata-path` 必须指向 Stata 安装目录，并且该目录下应存在 `utilities/` 子目录。

PowerShell:

```powershell
$env:STATA_PATH = "D:\Stata17"
```

Git Bash:

```bash
export STATA_PATH="D:/Stata17"
```

如果不给 `STATA_PATH`，引擎会尝试走 `pystata.config.init(edition)` 的默认初始化。

### 3.2 edition 参数

支持：`mp`、`se`、`be`。

示例：

```bash
uv run python main.py --edition mp
```

## 4. 如何使用

### 4.1 快速自检（Smoke Test）

```bash
uv run python main.py --edition mp
```

默认会执行：

- `sysuse auto, clear`
- `summarize price weight mpg`
- `regress price weight mpg`

执行日志会写到 `logs/`，并将日志内容打印到终端。

### 4.2 执行你自己的 do 文件

```bash
uv run python main.py --script ./path/to/analysis.do --edition mp
```

机器调用建议打开 JSON 输出并按退出码判断成功/失败：

```bash
uv run python main.py --script ./path/to/analysis.do --edition mp --json
```

### 4.3 在代码中调用（给 Agent 使用）

```python
from pathlib import Path

from infra import StataConfig, StataEngine

cfg = StataConfig(
		edition="mp",
		stata_path="D:/Stata17",  # 可选，不填则走默认初始化
		working_dir=Path.cwd(),
		log_dir=Path("logs"),
)

engine = StataEngine(cfg)

res1 = engine.run("sysuse auto, clear")
res2 = engine.run("regress price weight mpg")

# 导入/导出示例
res3 = engine.load_data("./data/input.csv")
res4 = engine.export_data("./data/output.dta")

print(res2.ok, res2.rc, res2.error_type)
print(res2.summary)
print(res2.log_path)
print(engine.get_output())  # 等价于最近一次执行结果的 log_tail
engine.close()
```

## 5. 常见问题

- 报错：`path is invalid`
  说明 `STATA_PATH` 层级不对，检查你填写的目录下是否有 `utilities/`。

- 报错：`license` 相关
  说明 Stata 授权不可用，先确认本机 Stata GUI 可正常启动并已激活。

- 报错：`No module named pystata`
  通常是 Stata 初始化未成功把 `utilities` 加入 Python 路径，优先检查 `STATA_PATH` 是否指向包含 `utilities/` 的目录。

## 6. 后续扩展建议

本地 MVP 稳定后，建议把 `StataEngine` 直接封装成 MCP 工具层，最小先做 3 个工具：

- `run_cmds`
- `run_do`
- `get_last_output`
