---
name: stata-job-runner
description: Run local Stata `.do` files or inline Stata commands through the StataAgent subprocess executor and inspect structured execution facts. Use when Codex needs to execute Stata code, collect `JobResult` JSON, inspect `run.log` or artifacts after failures, or drive this specific StataAgent execution infrastructure from another agent.
---

# Stata Job Runner

Use the local StataAgent repository as an execution service. This skill is a transitional control layer, not the final execution boundary; the long-term public interface should move to MCP. Focus on execution facts only: whether the job completed, where logs and artifacts live, and what phase failed. Do not judge whether coefficients or empirical results are economically sensible.

## Quick Start

- Prefer the installed wrapper script `scripts/run_stata_job.py` instead of rebuilding the CLI by hand.
- Resolve the wrapper script to an absolute path from the installed skill directory before executing it. Do not assume `python scripts/run_stata_job.py ...` works from an arbitrary cwd.
- If this skill is globally installed in the default Codex location on Windows, the wrapper path is typically `C:\Users\JinYu\.codex\skills\stata-job-runner\scripts\run_stata_job.py`. If the harness exposes a different installed skill path, use that path instead.
- Use `run-do` for an existing `.do` file.
- Use `run-inline` for short generated commands, probes, or smoke tests.
- Pass an explicit `--working-dir` whenever relative paths matter.
- Treat the returned JSON as the source of truth. Open `run.log` only when `summary`, `error_kind`, or `log_tail` are insufficient.
- Read [references/contract.md](references/contract.md) when you need the exact wrapper flags, path resolution order, runner fallback behavior, or `JobResult` field meanings.
- Read [references/mcp-contract.md](references/mcp-contract.md) when you need the future MCP-facing tool contract this skill is converging toward.

## Workflow

1. Resolve inputs.
   - Resolve the installed skill directory first, then call its wrapper script by absolute path.
   - Use `--repo-root` whenever the current directory is not the StataAgent repo and auto-detection may not find the clone.
   - Always pass `--stata-path`. The runner does not perform environment-variable or Windows auto-discovery fallback.
   - Set `--working-dir` to the directory that owns relative scripts, data, and outputs.
2. Execute the job.
   - Existing script:
     ```bash
     python "C:\Users\JinYu\.codex\skills\stata-job-runner\scripts\run_stata_job.py" run-do "D:\path\analysis.do" --working-dir "D:\path"
     ```
   - Inline commands:
     ```bash
     python "C:\Users\JinYu\.codex\skills\stata-job-runner\scripts\run_stata_job.py" run-inline "sysuse auto, clear\nregress price weight mpg" --working-dir "D:\work"
     ```
3. Read the result.
   - `status="succeeded"` means Stata exited cleanly.
   - `status="failed"` means inspect `phase`, `error_kind`, `summary`, `diagnostic_excerpt`, and `error_signature` first.
   - `artifacts` may contain partial outputs even when the run failed.
4. Escalate detail only when needed.
   - Read `run_log_path` for the full Stata transcript.
   - Read `process_log_path` only when it exists; it is optional and only retained when it contains information not already captured in `run.log`.

## Boundaries

- Keep this skill at the execution layer.
- Do not interpret empirical validity, coefficient signs, or research design quality here.
- Do not bypass the wrapper script unless you need a new CLI flag that the wrapper does not expose yet.
- Treat this skill as a bridge to the future MCP surface, not as the permanent home of execution logic.
