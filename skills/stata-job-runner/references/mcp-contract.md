# MCP Contract

This skill is a transitional wrapper around the local StataAgent repository. The intended long-term public boundary is an MCP server that exposes the same execution semantics without forcing other agents to know the repo layout.

## Target Tools

- `run_do`
- `run_inline`

## Target Inputs

`run_do`:

- `script_path`
- `working_dir`
- `timeout_sec`
- `artifact_globs`
- `env_overrides`
- `stata_path` as an optional explicit override

`run_inline`:

- `commands`
- `working_dir`
- `timeout_sec`
- `artifact_globs`
- `env_overrides`
- `stata_path` as an optional explicit override

## Target Output

The MCP output should mirror the current `JobResult` contract plus the existing mechanical diagnostics:

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

## Boundary

- Keep execution results factual and mechanically derived.
- Do not add economic interpretation, regression summaries, or repair suggestions at the MCP layer.
- Preserve CLI and MCP result semantics so the skill can become a thin usage guide rather than a second execution implementation.
