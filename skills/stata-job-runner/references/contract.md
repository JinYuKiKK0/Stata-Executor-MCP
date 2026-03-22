# Contract

## Repository Resolution

`scripts/run_stata_job.py` resolves the StataAgent repo root in this order:

1. `--repo-root`
2. `STATA_AGENT_ROOT`
3. The current directory or one of its parents if it contains `main.py` and `infra/__init__.py`
4. `D:\Developments\PythonProject\StataAgent`

Pass `--repo-root` whenever the local clone lives elsewhere.
Call the wrapper script itself by absolute path from the installed skill directory; do not rely on a cwd-relative `scripts/run_stata_job.py`.

## UV Resolution

The wrapper resolves its Python runner in this order:

1. `--uv-path`
2. `UV_EXE`
3. `shutil.which("uv")`
4. WinGet package fallbacks under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\*\uv.exe`
5. Repo-local `.venv` Python under `<repo>/.venv`

It validates each candidate before using it. If no `uv` candidate works but the repo has a working `.venv`, the wrapper falls back to that interpreter and runs `main.py` directly. If neither exists, the wrapper returns a stable bootstrap-style JSON failure instead of silently falling back to arbitrary system Python.

## Stata Resolution

The underlying runner resolves the Stata executable only from `--stata-path`.

If `--stata-path` is missing or points to a non-existent target, the runner returns a stable `bootstrap_error` JSON payload. It does not read `STATA_PATH`/`STATA_EXE`, Windows registry, or common install directories.

## Command Surface

Use one of these forms:

```bash
python "C:\Users\JinYu\.codex\skills\stata-job-runner\scripts\run_stata_job.py" run-do "D:\path\analysis.do" --working-dir "D:\path"
python "C:\Users\JinYu\.codex\skills\stata-job-runner\scripts\run_stata_job.py" run-inline "display 1" --working-dir "D:\path"
```

Common flags:

- `--stata-path`: Stata executable path or install directory
- `--working-dir`: Base directory for relative scripts, data, and output files
- `--job-root`: Directory that stores per-job manifests and logs
- `--timeout-sec`: Hard timeout for one job
- `--artifact-glob`: Repeatable relative glob for artifact collection
- `--env KEY=VALUE`: Repeatable environment override
- `--compact`: Emit compact JSON instead of pretty JSON

For `run-inline`, omit the `commands` positional argument to stream commands through stdin.

## Output Contract

The wrapper relays the project's `JobResult` JSON. Treat these fields as the stable interface:

- `status`: `succeeded` or `failed`
- `phase`: `bootstrap`, `input`, `execute`, `collect`, or `completed`
- `exit_code`: Stata or CLI exit code
- `error_kind`: Stable machine label for the failure class
- `summary`: Short human-readable diagnosis
- `job_dir`: Per-run artifact directory
- `run_log_path`: Main Stata log
- `process_log_path`: Optional outer process log with unique information
- `log_tail`: Tail of the execution log for quick inspection
- `diagnostic_excerpt`: Mechanically extracted diagnostic block for quick triage
- `error_signature`: First high-signal failure line when a job fails
- `failed_command`: Most recent Stata command block when it can be identified
- `artifacts`: Collected outputs, including partial outputs on failure
- `elapsed_ms`: Wall-clock runtime
- `working_dir`: Effective base directory used by the job

## Reading Results

Follow this order:

1. Read `status`, `phase`, `exit_code`, `error_kind`, and `summary`.
2. Read `diagnostic_excerpt`, `error_signature`, and `failed_command`.
3. Read `log_tail` if the failure is still ambiguous.
4. Open `run_log_path` only when the structured fields are not enough.
5. Open `process_log_path` only when it exists.

Do not use this skill to decide whether regression output is economically correct. That belongs to a higher analysis layer.
