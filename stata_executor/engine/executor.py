from __future__ import annotations

from dataclasses import replace

from stata_executor.contract import (
    DoctorResult,
    Edition,
    ErrorKind,
    ExecutionResult,
    RunDoRequest,
    RunInlineRequest,
)
from stata_executor.runtime import ResolvedRuntime, RuntimeBootstrapError, prepare_runtime
from stata_executor.runtime.executable_resolver import build_stata_command, resolve_stata_executable

from .artifacts import collect_artifacts, snapshot_artifacts
from .doctor import build_doctor_result
from .output_parser import (
    classify_execution_failure,
    extract_diagnostics,
    parse_exit_code,
    render_result_text,
    strip_agent_rc_trailer_text,
)
from .preparation import (
    resolve_user_path,
    stage_do_input,
    stage_inline_input,
    validate_request,
    write_wrapper_do,
)
from .process_runner import run_subprocess


class StataExecutor:
    """Zero-dependency Stata execution kernel with CLI and MCP adapters layered on top."""

    def doctor(
        self,
        *,
        stata_executable: str | None = None,
        edition: Edition | None = None,
    ) -> DoctorResult:
        return build_doctor_result(
            stata_executable=stata_executable,
            edition=edition,
        )

    def run_do(self, request: RunDoRequest) -> ExecutionResult:
        validation_error = validate_request(
            request.timeout_sec,
            request.artifact_globs,
            working_dir=request.working_dir,
            script_path=request.script_path,
        )
        if validation_error is not None:
            return self._make_failed_result(
                error_kind="input_error",
                diagnostic_excerpt=validation_error,
            )

        try:
            runtime = prepare_runtime(request)
        except RuntimeBootstrapError as exc:
            return self._make_failed_result(
                error_kind="bootstrap_error",
                diagnostic_excerpt=str(exc),
            )

        script = resolve_user_path(request.script_path, runtime.working_dir)
        if not script.exists():
            return self._persist_result(
                runtime,
                self._make_failed_result(
                    error_kind="input_error",
                    diagnostic_excerpt=f"Script does not exist: {script}",
                ),
            )

        stage_do_input(runtime, script)
        return self._execute_prepared_job(runtime)

    def run_inline(self, request: RunInlineRequest) -> ExecutionResult:
        validation_error = validate_request(
            request.timeout_sec,
            request.artifact_globs,
            working_dir=request.working_dir,
        )
        if validation_error is not None:
            return self._make_failed_result(
                error_kind="input_error",
                diagnostic_excerpt=validation_error,
            )
        if not request.commands.strip():
            return self._make_failed_result(
                error_kind="input_error",
                diagnostic_excerpt="Inline execution requires non-empty commands.",
            )

        try:
            runtime = prepare_runtime(request)
        except RuntimeBootstrapError as exc:
            return self._make_failed_result(
                error_kind="bootstrap_error",
                diagnostic_excerpt=str(exc),
            )

        stage_inline_input(runtime, request.commands)
        return self._execute_prepared_job(runtime)

    def _execute_prepared_job(self, runtime: ResolvedRuntime) -> ExecutionResult:
        write_wrapper_do(runtime)
        before_snapshot = snapshot_artifacts(runtime.working_dir, runtime.artifact_globs)
        result = self._run_subprocess_job(runtime)
        try:
            artifacts = collect_artifacts(
                runtime.working_dir, runtime.artifact_globs, before_snapshot
            )
        except OSError as exc:
            note = f"artifact collection partially failed: {exc}"
            merged_excerpt = (
                f"{result.diagnostic_excerpt}\n{note}".strip()
                if result.diagnostic_excerpt
                else note
            )
            return self._persist_result(
                runtime,
                replace(result, diagnostic_excerpt=merged_excerpt, artifacts=[]),
            )

        if result.status == "failed":
            return self._persist_result(runtime, replace(result, artifacts=artifacts))
        return self._persist_result(
            runtime,
            replace(result, status="succeeded", error_kind=None, artifacts=artifacts),
        )

    def _run_subprocess_job(self, runtime: ResolvedRuntime) -> ExecutionResult:
        executable = resolve_stata_executable(
            runtime.config.stata_executable, runtime.config.edition
        )
        if executable is None:
            return self._make_failed_result(
                error_kind="bootstrap_error",
                diagnostic_excerpt="Unable to resolve a Stata executable from explicit input.",
            )

        outcome = run_subprocess(runtime, build_stata_command(executable, runtime.wrapper_do_path))
        if outcome.timed_out:
            return self._make_failed_result(
                error_kind="timeout",
                result_text=render_result_text(outcome.primary_text),
                diagnostic_excerpt=extract_diagnostics(outcome.primary_text, exit_code=124)
                or f"Execution timed out after {runtime.timeout_sec}s and the subprocess was terminated.",
            )

        if outcome.start_error is not None:
            return self._make_failed_result(
                error_kind="bootstrap_error",
                diagnostic_excerpt=f"Failed to start Stata subprocess: {outcome.start_error}",
            )

        exit_code = parse_exit_code(outcome.primary_text, fallback=outcome.returncode)
        result_text = render_result_text(outcome.primary_text)
        diagnostic_excerpt = extract_diagnostics(outcome.primary_text, exit_code)

        if outcome.returncode != 0 and not outcome.primary_text.strip():
            fallback_text = outcome.process_text or outcome.process_output
            return self._make_failed_result(
                error_kind="bootstrap_error",
                result_text=render_result_text(fallback_text),
                diagnostic_excerpt=strip_agent_rc_trailer_text(fallback_text),
            )

        error_kind = (
            None if exit_code == 0 else classify_execution_failure(outcome.primary_text, exit_code)
        )
        return ExecutionResult(
            status="succeeded" if exit_code == 0 else "failed",
            error_kind=error_kind,
            result_text=result_text,
            diagnostic_excerpt=diagnostic_excerpt,
            artifacts=[],
        )

    def _make_failed_result(
        self,
        *,
        error_kind: ErrorKind,
        result_text: str = "",
        diagnostic_excerpt: str = "",
    ) -> ExecutionResult:
        return ExecutionResult(
            status="failed",
            error_kind=error_kind,
            result_text=result_text,
            diagnostic_excerpt=diagnostic_excerpt,
            artifacts=[],
        )

    def _persist_result(self, runtime: ResolvedRuntime, result: ExecutionResult) -> ExecutionResult:
        runtime.result_path.write_text(result.to_json(pretty=True), encoding="utf-8")
        return result
