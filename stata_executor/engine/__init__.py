from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import shutil
import subprocess
import time

from ..contract import ConfigSource, DoctorResult, Edition, ExecutionResult, ExecutorDefaults, RunDoRequest, RunInlineRequest
from ..runtime import RuntimeBootstrapError, ResolvedRuntime, prepare_runtime, resolve_configuration
from ..runtime.executable_resolver import build_stata_command, resolve_stata_executable


class StataExecutor:
    """Zero-dependency Stata execution kernel with CLI and MCP adapters layered on top."""

    def doctor(
        self,
        *,
        stata_executable: str | None = None,
        edition: Edition | None = None,
        config_source: ConfigSource | None = None,
    ) -> DoctorResult:
        try:
            resolved = resolve_configuration(
                stata_executable=stata_executable,
                edition=edition,
                source_override=config_source if config_source in {"explicit", "env", "missing"} else None,
            )
        except RuntimeBootstrapError as exc:
            return DoctorResult(
                ready=False,
                summary=f"Runtime configuration is invalid: {exc}",
                config_path="",
                config_exists=False,
                config_source="explicit" if stata_executable else "missing",
                stata_executable=stata_executable,
                edition=edition,
                defaults=ExecutorDefaults(),
                errors=[str(exc)],
            )

        executable = resolve_stata_executable(resolved.stata_executable, resolved.edition)
        if resolved.stata_executable is None:
            return DoctorResult(
                ready=False,
                summary="No Stata executable configured. Pass it explicitly via CLI argument or MCP environment.",
                config_path=str(resolved.config_path),
                config_exists=resolved.config_exists,
                config_source=resolved.config_source,
                stata_executable=None,
                edition=resolved.edition,
                defaults=resolved.defaults,
                errors=["Missing explicit 'stata_executable' input."],
            )
        if executable is None:
            return DoctorResult(
                ready=False,
                summary="Configured Stata executable could not be resolved.",
                config_path=str(resolved.config_path),
                config_exists=resolved.config_exists,
                config_source=resolved.config_source,
                stata_executable=resolved.stata_executable,
                edition=resolved.edition,
                defaults=resolved.defaults,
                errors=[f"Path does not resolve to a usable Stata executable: {resolved.stata_executable}"],
            )

        return DoctorResult(
            ready=True,
            summary="Stata executable resolved successfully.",
            config_path=str(resolved.config_path),
            config_exists=resolved.config_exists,
            config_source=resolved.config_source,
            stata_executable=str(executable),
            edition=resolved.edition,
            defaults=resolved.defaults,
            errors=[],
        )

    def run_do(self, request: RunDoRequest) -> ExecutionResult:
        validation_error = self._validate_request(request.timeout_sec, request.artifact_globs)
        if validation_error is not None:
            return self._standalone_result(
                phase="input",
                exit_code=2,
                error_kind="input_error",
                summary=validation_error,
                working_dir=request.working_dir,
            )

        try:
            runtime = prepare_runtime(request)
        except RuntimeBootstrapError as exc:
            return self._standalone_result(
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary=str(exc),
                working_dir=request.working_dir,
            )

        script = self._resolve_user_path(request.script_path, runtime.working_dir)
        if not script.exists():
            return self._finalize_result(
                runtime,
                self._result(
                    runtime,
                    phase="input",
                    exit_code=601,
                    error_kind="input_error",
                    summary=f"Script does not exist: {script}",
                ),
            )

        shutil.copy2(script, runtime.input_do_path)
        return self._execute_prepared_job(runtime)

    def run_inline(self, request: RunInlineRequest) -> ExecutionResult:
        validation_error = self._validate_request(request.timeout_sec, request.artifact_globs)
        if validation_error is not None:
            return self._standalone_result(
                phase="input",
                exit_code=2,
                error_kind="input_error",
                summary=validation_error,
                working_dir=request.working_dir,
            )
        if not request.commands.strip():
            return self._standalone_result(
                phase="input",
                exit_code=2,
                error_kind="input_error",
                summary="Inline execution requires non-empty commands.",
                working_dir=request.working_dir,
            )

        try:
            runtime = prepare_runtime(request)
        except RuntimeBootstrapError as exc:
            return self._standalone_result(
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary=str(exc),
                working_dir=request.working_dir,
            )

        normalized = request.commands if request.commands.endswith("\n") else f"{request.commands}\n"
        runtime.input_do_path.write_text(normalized, encoding="utf-8")
        return self._execute_prepared_job(runtime)

    def _execute_prepared_job(self, runtime: ResolvedRuntime) -> ExecutionResult:
        self._write_wrapper_do(runtime)
        before_snapshot = self._snapshot_artifacts(runtime.working_dir, runtime.artifact_globs)
        result = self._run_subprocess_job(runtime)
        try:
            artifacts = self._collect_artifacts(runtime.working_dir, runtime.artifact_globs, before_snapshot)
        except OSError as exc:
            if result.status == "failed":
                return self._finalize_result(
                    runtime,
                    replace(
                        result,
                        summary=f"{result.summary} Artifact collection also failed: {exc}",
                        artifacts=[],
                    ),
                )
            return self._finalize_result(
                runtime,
                replace(
                    result,
                    status="failed",
                    phase="collect",
                    error_kind="artifact_collection_error",
                    summary=f"Artifact collection failed: {exc}",
                    artifacts=[],
                ),
            )

        if result.status == "failed":
            return self._finalize_result(runtime, replace(result, artifacts=artifacts))
        return self._finalize_result(
            runtime,
            replace(
                result,
                status="succeeded",
                phase="completed",
                error_kind=None,
                artifacts=artifacts,
            ),
        )

    def _run_subprocess_job(self, runtime: ResolvedRuntime) -> ExecutionResult:
        started_at = time.monotonic()
        executable = resolve_stata_executable(runtime.config.stata_executable, runtime.config.edition)
        if executable is None:
            return self._result(
                runtime,
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary="Unable to resolve a Stata executable from explicit input.",
            )

        command = build_stata_command(executable, runtime.wrapper_do_path)
        try:
            completed = subprocess.run(
                command,
                cwd=runtime.job_dir,
                env=runtime.env,
                capture_output=True,
                text=True,
                timeout=runtime.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            _, process_text = self._finalize_process_log(
                runtime,
                self._compose_process_output(exc.stdout, exc.stderr),
            )
            run_text = self._read_text(runtime.run_log_path)
            primary_text = run_text or process_text
            result_text = self._render_result_text(primary_text)
            diagnostic_excerpt, error_signature, failed_command = self._extract_diagnostics(primary_text, exit_code=124)
            return self._result(
                runtime,
                phase="execute",
                exit_code=124,
                error_kind="timeout",
                summary=f"Execution timed out after {runtime.timeout_sec}s and the subprocess was terminated.",
                result_text=result_text,
                diagnostic_excerpt=diagnostic_excerpt,
                error_signature=error_signature,
                failed_command=failed_command,
                elapsed_ms=elapsed_ms,
            )
        except OSError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return self._result(
                runtime,
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary=f"Failed to start Stata subprocess: {exc}",
                result_text="",
                elapsed_ms=elapsed_ms,
            )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        process_output = self._compose_process_output(completed.stdout, completed.stderr)
        _, process_text = self._finalize_process_log(runtime, process_output)
        run_text = self._read_text(runtime.run_log_path)
        primary_text = run_text or process_text
        exit_code = self._parse_exit_code(primary_text, fallback=completed.returncode)
        result_text = self._render_result_text(primary_text)
        diagnostic_excerpt, error_signature, failed_command = self._extract_diagnostics(primary_text, exit_code)

        if completed.returncode != 0 and not primary_text.strip():
            fallback_text = process_text or process_output
            return self._result(
                runtime,
                phase="bootstrap",
                exit_code=completed.returncode or 1,
                error_kind="bootstrap_error",
                summary=self._build_bootstrap_summary(process_output),
                result_text=self._render_result_text(fallback_text),
                diagnostic_excerpt=self._strip_agent_rc_trailer_text(fallback_text),
                error_signature=self._extract_last_meaningful_line(fallback_text),
                elapsed_ms=elapsed_ms,
            )

        error_kind = None if exit_code == 0 else self._classify_execution_failure(primary_text, exit_code)
        return self._result(
            runtime,
            phase="completed" if exit_code == 0 else "execute",
            exit_code=exit_code,
            error_kind=error_kind,
            summary=self._build_execution_summary(primary_text, exit_code),
            result_text=result_text,
            diagnostic_excerpt=diagnostic_excerpt,
            error_signature=error_signature,
            failed_command=failed_command,
            elapsed_ms=elapsed_ms,
            status="succeeded" if exit_code == 0 else "failed",
        )

    def _write_wrapper_do(self, runtime: ResolvedRuntime) -> None:
        wrapper = "\n".join(
            [
                "version 17.0",
                "clear all",
                "set more off",
                "capture log close _all",
                f'log using "{runtime.run_log_path.as_posix()}", replace text name(agentlog)',
                f'cd "{runtime.working_dir.as_posix()}"',
                f'capture noisily do "{runtime.input_do_path.as_posix()}"',
                "local agent_rc = _rc",
                'display "__AGENT_RC__=`agent_rc\'"',
                "capture log close agentlog",
                "exit `agent_rc', STATA clear",
                "",
            ]
        )
        runtime.wrapper_do_path.write_text(wrapper, encoding="utf-8")

    def _result(
        self,
        runtime: ResolvedRuntime,
        *,
        phase: str,
        exit_code: int,
        error_kind: str | None,
        summary: str,
        result_text: str = "",
        diagnostic_excerpt: str = "",
        error_signature: str | None = None,
        failed_command: str | None = None,
        artifacts: list[str] | None = None,
        elapsed_ms: int = 0,
        status: str = "failed",
    ) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            phase=phase,
            exit_code=exit_code,
            error_kind=error_kind,
            summary=summary,
            result_text=result_text,
            diagnostic_excerpt=diagnostic_excerpt,
            error_signature=error_signature,
            failed_command=failed_command,
            artifacts=artifacts or [],
            elapsed_ms=elapsed_ms,
        )

    def _standalone_result(
        self,
        *,
        phase: str,
        exit_code: int,
        error_kind: str,
        summary: str,
        working_dir: str | None,
    ) -> ExecutionResult:
        return ExecutionResult(
            status="failed",
            phase=phase,
            exit_code=exit_code,
            error_kind=error_kind,
            summary=summary,
            result_text="",
            diagnostic_excerpt="",
            error_signature=None,
            failed_command=None,
            artifacts=[],
            elapsed_ms=0,
        )

    def _finalize_result(self, runtime: ResolvedRuntime, result: ExecutionResult) -> ExecutionResult:
        runtime.result_path.write_text(result.to_json(pretty=True), encoding="utf-8")
        return result

    def _validate_request(self, timeout_sec: int | None, artifact_globs: tuple[str, ...]) -> str | None:
        if timeout_sec is not None and timeout_sec <= 0:
            return "timeout_sec must be a positive integer when provided."
        if any(Path(pattern).is_absolute() for pattern in artifact_globs):
            return "artifact_globs must be relative to working_dir."
        return None

    def _resolve_user_path(self, path_like: str, working_dir: Path) -> Path:
        path = Path(path_like)
        if not path.is_absolute():
            path = working_dir / path
        return path.resolve()

    def _snapshot_artifacts(self, working_dir: Path, patterns: tuple[str, ...]) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        for path in self._iter_artifact_matches(working_dir, patterns):
            stat = path.stat()
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _collect_artifacts(
        self,
        working_dir: Path,
        patterns: tuple[str, ...],
        before_snapshot: dict[str, tuple[int, int]],
    ) -> list[str]:
        artifacts: list[str] = []
        seen: set[str] = set()
        for path in self._iter_artifact_matches(working_dir, patterns):
            resolved = str(path.resolve())
            stat = path.stat()
            marker = (stat.st_mtime_ns, stat.st_size)
            if before_snapshot.get(resolved) != marker and resolved not in seen:
                artifacts.append(resolved)
                seen.add(resolved)
        artifacts.sort()
        return artifacts

    def _iter_artifact_matches(self, working_dir: Path, patterns: tuple[str, ...]) -> list[Path]:
        matches: list[Path] = []
        for pattern in patterns:
            matches.extend(path for path in working_dir.glob(pattern) if path.is_file())
        return sorted(matches)

    def _finalize_process_log(self, runtime: ResolvedRuntime, process_output: str) -> tuple[str | None, str]:
        raw_text = self._read_text(runtime.raw_process_log_path)
        run_text = self._read_text(runtime.run_log_path)

        if raw_text:
            normalized_raw = self._normalize_for_dedup(raw_text)
            normalized_run = self._normalize_for_dedup(run_text)
            if normalized_run and normalized_run in normalized_raw:
                runtime.raw_process_log_path.unlink(missing_ok=True)
                return None, ""

            if runtime.raw_process_log_path != runtime.process_log_path:
                if runtime.process_log_path.exists():
                    runtime.process_log_path.unlink()
                runtime.raw_process_log_path.replace(runtime.process_log_path)
            return str(runtime.process_log_path), raw_text

        if process_output.strip():
            runtime.process_log_path.write_text(process_output, encoding="utf-8")
            return str(runtime.process_log_path), process_output

        return None, ""

    def _parse_exit_code(self, text: str, fallback: int) -> int:
        match = re.findall(r"__AGENT_RC__\s*=\s*(\d+)", text)
        if match:
            return int(match[-1])
        generic = re.findall(r"r\((\d+)\)", text)
        if generic:
            return int(generic[-1])
        return fallback

    def _classify_execution_failure(self, text: str, exit_code: int) -> str:
        low = text.lower()
        if exit_code in {198, 199}:
            return "stata_parse_or_command_error"
        if "invalid syntax" in low or "unrecognized" in low:
            return "stata_parse_or_command_error"
        return "stata_runtime_error"

    def _build_execution_summary(self, text: str, exit_code: int) -> str:
        if exit_code == 0:
            return "Stata do-file completed successfully."

        error_signature = self._extract_error_signature(text, exit_code)
        if error_signature:
            return f"Stata execution failed with exit_code={exit_code}: {error_signature}"
        return f"Stata execution failed with exit_code={exit_code}."

    def _build_bootstrap_summary(self, text: str) -> str:
        stripped = [line.strip() for line in text.splitlines() if line.strip()]
        if stripped:
            return f"Stata subprocess bootstrap failed: {stripped[-1]}"
        return "Stata subprocess bootstrap failed before any execution log was created."

    def _compose_process_output(self, stdout: str | None, stderr: str | None) -> str:
        parts = [chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()]
        return "\n".join(parts)

    def _normalize_for_dedup(self, text: str) -> str:
        return "\n".join(line.rstrip() for line in text.splitlines()).strip()

    def _read_text(self, path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    def _render_result_text(self, text: str) -> str:
        if not text:
            return ""

        command_echo_pattern = re.compile(r"^\.\s*$|^\.\s+\S")
        numbered_line_pattern = re.compile(r"^\s*\d+\.\s")
        continuation_pattern = re.compile(r"^>\s")
        log_info_pattern = re.compile(
            r"^\s*(name:|log:|log type:|opened on:|closed on:|Log file saved to:)",
            re.IGNORECASE,
        )

        filtered: list[str] = []
        previous_blank = False
        for line in self._strip_agent_rc_trailer(text.splitlines()):
            if command_echo_pattern.match(line):
                continue
            if numbered_line_pattern.match(line):
                continue
            if continuation_pattern.match(line):
                continue
            if log_info_pattern.match(line):
                continue

            is_blank = not line.strip()
            if is_blank:
                if previous_blank:
                    continue
                filtered.append("")
                previous_blank = True
                continue

            filtered.append(line.rstrip())
            previous_blank = False

        while filtered and not filtered[-1].strip():
            filtered.pop()
        blocks = self._extract_empirical_result_blocks(filtered)
        if blocks:
            return "\n\n".join(blocks)
        return "\n".join(filtered)

    def _extract_empirical_result_blocks(self, lines: list[str]) -> list[str]:
        blocks: list[str] = []
        index = 0

        while index < len(lines):
            line = lines[index]

            if re.search(r"\bregression\b", line, re.IGNORECASE) and "Number of obs" in line:
                block_lines = [line.rstrip()]
                index += 1
                while index < len(lines):
                    current = lines[index].rstrip()
                    block_lines.append(current)
                    if lines[index].startswith("F test that all u_i=0:"):
                        break
                    index += 1
                block = "\n".join(block_lines).strip()
                if block:
                    blocks.append(block)
                index += 1
                continue

            if re.match(r"^\s*Variable\s+\|\s+Obs", line):
                block_lines = [line.rstrip()]
                index += 1
                while index < len(lines):
                    current = lines[index]
                    current_stripped = current.strip()
                    if not current_stripped:
                        break
                    if "|" in current or re.match(r"^-+[+-]-+$", current_stripped) or re.match(r"^-+$", current_stripped):
                        block_lines.append(current.rstrip())
                        index += 1
                        continue
                    break
                block = "\n".join(block_lines).strip()
                if block:
                    blocks.append(block)
                continue

            index += 1

        return blocks

    def _extract_diagnostics(self, text: str, exit_code: int) -> tuple[str, str | None, str | None]:
        if not text:
            return "", None, None
        if exit_code == 0:
            return "", None, None

        lines = text.splitlines()
        command_start, failed_command = self._extract_last_command_block(lines)
        error_index, error_signature = self._extract_error_signature_with_index(lines, exit_code)

        if command_start is not None and error_index is not None and command_start <= error_index:
            excerpt_start = command_start
        elif error_index is not None:
            excerpt_start = error_index
        elif command_start is not None:
            excerpt_start = command_start
        else:
            excerpt_start = 0

        excerpt_lines = self._strip_agent_rc_trailer(lines[excerpt_start:])
        excerpt = "\n".join(excerpt_lines).strip()
        return excerpt, error_signature, failed_command

    def _extract_last_command_block(self, lines: list[str]) -> tuple[int | None, str | None]:
        block_start: int | None = None
        block_lines: list[str] = []
        blocks: list[tuple[int, str]] = []

        for index, raw_line in enumerate(lines):
            if raw_line.startswith(". "):
                if block_start is not None and block_lines:
                    blocks.append((block_start, "\n".join(block_lines).strip()))
                block_start = index
                block_lines = [raw_line[2:].rstrip()]
                continue

            if raw_line.startswith("> ") and block_start is not None:
                block_lines.append(raw_line[2:].rstrip())

        if block_start is not None and block_lines:
            blocks.append((block_start, "\n".join(block_lines).strip()))

        if not blocks:
            return None, None
        return blocks[-1]

    def _extract_error_signature_with_index(self, lines: list[str], exit_code: int) -> tuple[int | None, str | None]:
        if exit_code == 0:
            return None, None

        final_rc_index: int | None = None
        for index in range(len(lines) - 1, -1, -1):
            stripped = lines[index].strip()
            if re.fullmatch(r"r\(\d+\);?", stripped):
                final_rc_index = index
                break

        search_end = final_rc_index if final_rc_index is not None else len(lines)
        for index in range(search_end - 1, -1, -1):
            stripped = lines[index].strip()
            if not stripped:
                continue
            if stripped.startswith("__AGENT_RC__") or stripped.startswith("r("):
                continue
            if lines[index].startswith(". ") or lines[index].startswith("> "):
                continue
            return index, stripped
        return None, None

    def _extract_error_signature(self, text: str, exit_code: int) -> str | None:
        _, signature = self._extract_error_signature_with_index(text.splitlines(), exit_code)
        return signature

    def _extract_last_meaningful_line(self, text: str) -> str | None:
        for raw_line in reversed(text.splitlines()):
            stripped = raw_line.strip()
            if stripped:
                return stripped
        return None

    def _strip_agent_rc_trailer(self, lines: list[str]) -> list[str]:
        trimmed = list(lines)
        while trimmed and trimmed[-1].strip().startswith("__AGENT_RC__"):
            trimmed.pop()
        return trimmed

    def _strip_agent_rc_trailer_text(self, text: str) -> str:
        return "\n".join(self._strip_agent_rc_trailer(text.splitlines())).strip()


def run_do(request: RunDoRequest) -> ExecutionResult:
    return StataExecutor().run_do(request)


def run_inline(request: RunInlineRequest) -> ExecutionResult:
    return StataExecutor().run_inline(request)


def doctor(*, stata_executable: str | None = None, edition: Edition | None = None) -> DoctorResult:
    return StataExecutor().doctor(stata_executable=stata_executable, edition=edition)
