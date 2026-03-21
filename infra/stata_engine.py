from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .config import StataConfig
from .executable_resolver import build_stata_command, resolve_stata_executable
from .models import ErrorKind, JobResult, JobSpec


@dataclass(slots=True)
class _JobContext:
    working_dir: Path
    job_dir: Path
    input_do_path: Path
    wrapper_do_path: Path
    run_log_path: Path
    raw_process_log_path: Path
    process_log_path: Path
    result_path: Path
    timeout_sec: int
    artifact_globs: tuple[str, ...]
    env: dict[str, str]


class StataJobRunner:
    """Isolated Stata job runner backed only by a subprocess Stata invocation."""

    def __init__(self, config: StataConfig):
        self.config = config
        self._job_root = config.resolve_job_root()

    def run_do(self, script_path: str | Path, spec: JobSpec | None = None) -> JobResult:
        job = self._create_job_context(spec)
        script = self._resolve_user_path(script_path, job.working_dir)
        if not script.exists():
            return self._finalize_result(
                job,
                JobResult(
                    status="failed",
                    phase="input",
                    exit_code=601,
                    error_kind="input_error",
                    summary=f"Script does not exist: {script}",
                    job_dir=str(job.job_dir),
                    run_log_path=None,
                    process_log_path=None,
                    log_tail="",
                    artifacts=[],
                    elapsed_ms=0,
                    working_dir=str(job.working_dir),
                ),
            )

        shutil.copy2(script, job.input_do_path)
        return self._execute_prepared_job(job)

    def run_inline(self, commands: str, spec: JobSpec | None = None) -> JobResult:
        job = self._create_job_context(spec)
        normalized = commands if commands.endswith("\n") else f"{commands}\n"
        job.input_do_path.write_text(normalized, encoding="utf-8")
        return self._execute_prepared_job(job)

    def _create_job_context(self, spec: JobSpec | None) -> _JobContext:
        effective_spec = spec or JobSpec()
        working_dir = effective_spec.resolve_working_dir(self.config)
        timeout_sec = effective_spec.resolve_timeout_sec(self.config)
        artifact_globs = effective_spec.resolve_artifact_globs(self.config)
        env = effective_spec.resolve_env(self.config)
        job_dir = self._job_root / f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        job_dir.mkdir(parents=True, exist_ok=False)
        return _JobContext(
            working_dir=working_dir,
            job_dir=job_dir,
            input_do_path=job_dir / "input.do",
            wrapper_do_path=job_dir / "wrapper.do",
            run_log_path=job_dir / "run.log",
            raw_process_log_path=job_dir / "wrapper.log",
            process_log_path=job_dir / "process.log",
            result_path=job_dir / "result.json",
            timeout_sec=timeout_sec,
            artifact_globs=artifact_globs,
            env=env,
        )

    def _execute_prepared_job(self, job: _JobContext) -> JobResult:
        self._write_wrapper_do(job)
        before_snapshot = self._snapshot_artifacts(job.working_dir, job.artifact_globs)
        result = self._run_subprocess_job(job)
        try:
            artifacts = self._collect_artifacts(job.working_dir, job.artifact_globs, before_snapshot)
        except OSError as exc:
            if result.status == "failed":
                failed = JobResult(
                    status=result.status,
                    phase=result.phase,
                    exit_code=result.exit_code,
                    error_kind=result.error_kind,
                    summary=f"{result.summary} Artifact collection also failed: {exc}",
                    job_dir=result.job_dir,
                    run_log_path=result.run_log_path,
                    process_log_path=result.process_log_path,
                    log_tail=result.log_tail,
                    artifacts=[],
                    elapsed_ms=result.elapsed_ms,
                    working_dir=result.working_dir,
                )
                return self._finalize_result(job, failed)

            failed = JobResult(
                status="failed",
                phase="collect",
                exit_code=result.exit_code,
                error_kind="artifact_collection_error",
                summary=f"Artifact collection failed: {exc}",
                job_dir=result.job_dir,
                run_log_path=result.run_log_path,
                process_log_path=result.process_log_path,
                log_tail=result.log_tail,
                artifacts=[],
                elapsed_ms=result.elapsed_ms,
                working_dir=result.working_dir,
            )
            return self._finalize_result(job, failed)

        if result.status == "failed":
            failed = JobResult(
                status=result.status,
                phase=result.phase,
                exit_code=result.exit_code,
                error_kind=result.error_kind,
                summary=result.summary,
                job_dir=result.job_dir,
                run_log_path=result.run_log_path,
                process_log_path=result.process_log_path,
                log_tail=result.log_tail,
                artifacts=artifacts,
                elapsed_ms=result.elapsed_ms,
                working_dir=result.working_dir,
            )
            return self._finalize_result(job, failed)

        succeeded = JobResult(
            status="succeeded",
            phase="completed",
            exit_code=result.exit_code,
            error_kind=None,
            summary=result.summary,
            job_dir=result.job_dir,
            run_log_path=result.run_log_path,
            process_log_path=result.process_log_path,
            log_tail=result.log_tail,
            artifacts=artifacts,
            elapsed_ms=result.elapsed_ms,
            working_dir=result.working_dir,
        )
        return self._finalize_result(job, succeeded)

    def _run_subprocess_job(self, job: _JobContext) -> JobResult:
        started_at = time.monotonic()
        executable = resolve_stata_executable(self.config.stata_path, self.config.edition)
        if executable is None:
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary="Unable to resolve a Stata executable from stata_path and edition.",
                job_dir=str(job.job_dir),
                run_log_path=None,
                process_log_path=None,
                log_tail="",
                artifacts=[],
                elapsed_ms=0,
                working_dir=str(job.working_dir),
            )

        command = build_stata_command(executable, job.wrapper_do_path)
        try:
            completed = subprocess.run(
                command,
                cwd=job.job_dir,
                env=job.env,
                capture_output=True,
                text=True,
                timeout=job.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            process_log_path, process_text = self._finalize_process_log(job, self._compose_process_output(exc.stdout, exc.stderr))
            run_text = self._read_text(job.run_log_path)
            primary_text = run_text or process_text
            return JobResult(
                status="failed",
                phase="execute",
                exit_code=124,
                error_kind="timeout",
                summary=f"Execution timed out after {job.timeout_sec}s and the subprocess was terminated.",
                job_dir=str(job.job_dir),
                run_log_path=str(job.run_log_path) if job.run_log_path.exists() else None,
                process_log_path=process_log_path,
                log_tail=self._tail_text(primary_text),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                working_dir=str(job.working_dir),
            )
        except OSError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary=f"Failed to start Stata subprocess: {exc}",
                job_dir=str(job.job_dir),
                run_log_path=None,
                process_log_path=None,
                log_tail="",
                artifacts=[],
                elapsed_ms=elapsed_ms,
                working_dir=str(job.working_dir),
            )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        process_output = self._compose_process_output(completed.stdout, completed.stderr)
        process_log_path, process_text = self._finalize_process_log(job, process_output)
        run_text = self._read_text(job.run_log_path)
        primary_text = run_text or process_text
        exit_code = self._parse_exit_code(primary_text, fallback=completed.returncode)

        if completed.returncode != 0 and not primary_text.strip():
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=completed.returncode or 1,
                error_kind="bootstrap_error",
                summary=self._build_bootstrap_summary(process_output),
                job_dir=str(job.job_dir),
                run_log_path=None,
                process_log_path=process_log_path,
                log_tail=self._tail_text(process_text or process_output),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                working_dir=str(job.working_dir),
            )

        error_kind = None if exit_code == 0 else self._classify_execution_failure(primary_text, exit_code)
        return JobResult(
            status="succeeded" if exit_code == 0 else "failed",
            phase="completed" if exit_code == 0 else "execute",
            exit_code=exit_code,
            error_kind=error_kind,
            summary=self._build_execution_summary(primary_text, exit_code),
            job_dir=str(job.job_dir),
            run_log_path=str(job.run_log_path) if job.run_log_path.exists() else None,
            process_log_path=process_log_path,
            log_tail=self._tail_text(primary_text),
            artifacts=[],
            elapsed_ms=elapsed_ms,
            working_dir=str(job.working_dir),
        )

    def _write_wrapper_do(self, job: _JobContext) -> None:
        safe_run_log_path = job.run_log_path.as_posix()
        safe_working_dir = job.working_dir.as_posix()
        safe_input_path = job.input_do_path.as_posix()
        wrapper = "\n".join(
            [
                "version 17.0",
                "clear all",
                "set more off",
                "capture log close _all",
                f'log using "{safe_run_log_path}", replace text name(agentlog)',
                f'cd "{safe_working_dir}"',
                f'capture noisily do "{safe_input_path}"',
                "local agent_rc = _rc",
                'display "__AGENT_RC__=`agent_rc\'"',
                "capture log close agentlog",
                "exit `agent_rc', STATA clear",
                "",
            ]
        )
        job.wrapper_do_path.write_text(wrapper, encoding="utf-8")

    def _resolve_user_path(self, path_like: str | Path, working_dir: Path) -> Path:
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
            if Path(pattern).is_absolute():
                raise OSError(f"Artifact glob must be relative to working_dir: {pattern}")
            matches.extend(path for path in working_dir.glob(pattern) if path.is_file())
        return sorted(matches)

    def _finalize_process_log(self, job: _JobContext, process_output: str) -> tuple[str | None, str]:
        raw_text = self._read_text(job.raw_process_log_path)
        run_text = self._read_text(job.run_log_path)

        if raw_text:
            normalized_raw = self._normalize_for_dedup(raw_text)
            normalized_run = self._normalize_for_dedup(run_text)
            if normalized_run and normalized_run in normalized_raw:
                job.raw_process_log_path.unlink(missing_ok=True)
                return None, ""

            if job.raw_process_log_path != job.process_log_path:
                if job.process_log_path.exists():
                    job.process_log_path.unlink()
                job.raw_process_log_path.replace(job.process_log_path)
            return str(job.process_log_path), raw_text

        if process_output.strip():
            job.process_log_path.write_text(process_output, encoding="utf-8")
            return str(job.process_log_path), process_output

        return None, ""

    def _parse_exit_code(self, text: str, fallback: int) -> int:
        match = re.findall(r"__AGENT_RC__\s*=\s*(\d+)", text)
        if match:
            return int(match[-1])
        generic = re.findall(r"r\((\d+)\)", text)
        if generic:
            return int(generic[-1])
        return fallback

    def _classify_execution_failure(self, text: str, exit_code: int) -> ErrorKind:
        low = text.lower()
        if exit_code in {198, 199}:
            return "stata_parse_or_command_error"
        if "invalid syntax" in low or "unrecognized" in low:
            return "stata_parse_or_command_error"
        return "stata_runtime_error"

    def _build_execution_summary(self, text: str, exit_code: int) -> str:
        if exit_code == 0:
            return "Stata do-file completed successfully."

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("__AGENT_RC__") or line.startswith(". "):
                continue
            if line.startswith("r("):
                continue
            low = line.lower()
            if "invalid syntax" in low or "unrecognized" in low or "error" in low:
                return f"Stata execution failed with exit_code={exit_code}: {line}"

        generic = [line.strip() for line in text.splitlines() if line.strip().startswith("r(")]
        if generic:
            return f"Stata execution failed with exit_code={exit_code}: {generic[-1]}"
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

    def _tail_text(self, text: str, lines: int = 40) -> str:
        if not text:
            return ""
        split = text.splitlines()
        return "\n".join(split[-lines:])

    def _finalize_result(self, job: _JobContext, result: JobResult) -> JobResult:
        job.result_path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return result
