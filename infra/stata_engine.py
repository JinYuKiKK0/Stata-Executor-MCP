from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib import import_module
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Literal
import uuid

from .config import BackendType, StataConfig


JobStatus = Literal["succeeded", "failed"]
JobPhase = Literal["bootstrap", "input", "execute", "collect", "completed"]
ErrorKind = Literal[
    "bootstrap_error",
    "input_error",
    "timeout",
    "stata_parse_or_command_error",
    "stata_runtime_error",
    "artifact_collection_error",
]

_WINDOWS_EXECUTABLES: dict[str, tuple[str, ...]] = {
    "mp": ("StataMP-64.exe", "StataMP.exe"),
    "se": ("StataSE-64.exe", "StataSE.exe"),
    "be": ("StataBE-64.exe", "StataBE.exe"),
}


@dataclass(slots=True)
class JobSpec:
    """Execution-time overrides for one isolated Stata job."""

    working_dir: Path | str | None = None
    timeout_sec: int | None = None
    artifact_globs: tuple[str, ...] = ()
    env_overrides: dict[str, str] = field(default_factory=dict)

    def resolve_working_dir(self, config: StataConfig) -> Path:
        base = config.resolve_working_dir()
        if self.working_dir is None:
            return base
        path = Path(self.working_dir)
        if not path.is_absolute():
            path = base / path
        path = path.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_timeout_sec(self, config: StataConfig) -> int:
        return self.timeout_sec or config.default_timeout_sec

    def resolve_artifact_globs(self, config: StataConfig) -> tuple[str, ...]:
        if self.artifact_globs:
            return tuple(self.artifact_globs)
        return tuple(config.artifact_globs)

    def resolve_env(self, config: StataConfig) -> dict[str, str]:
        env = dict(os.environ)
        env.update(config.env_overrides)
        env.update(self.env_overrides)
        return env


@dataclass(slots=True)
class JobResult:
    """Serializable job manifest consumed by the agent layer."""

    status: JobStatus
    phase: JobPhase
    exit_code: int
    error_kind: ErrorKind | None
    summary: str
    job_dir: str
    log_path: str | None
    log_tail: str
    artifacts: list[str]
    elapsed_ms: int
    backend: BackendType
    working_dir: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass(slots=True)
class _JobContext:
    backend: BackendType
    working_dir: Path
    job_dir: Path
    input_do_path: Path
    wrapper_do_path: Path
    log_path: Path
    result_path: Path
    timeout_sec: int
    artifact_globs: tuple[str, ...]
    env: dict[str, str]


class StataJobRunner:
    """Isolated Stata job runner with subprocess backend by default."""

    def __init__(self, config: StataConfig):
        self.config = config
        self._job_root = config.resolve_job_root()
        self._default_working_dir = config.resolve_working_dir()
        self._pystata_ready = False

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
                    log_path=None,
                    log_tail="",
                    artifacts=[],
                    elapsed_ms=0,
                    backend=job.backend,
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
            backend=self.config.backend,
            working_dir=working_dir,
            job_dir=job_dir,
            input_do_path=job_dir / "input.do",
            wrapper_do_path=job_dir / "wrapper.do",
            log_path=job_dir / "run.log",
            result_path=job_dir / "result.json",
            timeout_sec=timeout_sec,
            artifact_globs=artifact_globs,
            env=env,
        )

    def _execute_prepared_job(self, job: _JobContext) -> JobResult:
        self._write_wrapper_do(job)
        before_snapshot = self._snapshot_artifacts(job.working_dir, job.artifact_globs)
        if job.backend == "subprocess":
            result = self._run_subprocess_job(job)
        elif job.backend == "pystata":
            result = self._run_pystata_job(job)
        else:
            raise ValueError(f"Unsupported backend: {job.backend}")

        if result.status == "failed":
            return self._finalize_result(job, result)

        try:
            artifacts = self._collect_artifacts(job.working_dir, job.artifact_globs, before_snapshot)
        except OSError as exc:
            result = JobResult(
                status="failed",
                phase="collect",
                exit_code=result.exit_code,
                error_kind="artifact_collection_error",
                summary=f"Artifact collection failed: {exc}",
                job_dir=result.job_dir,
                log_path=result.log_path,
                log_tail=result.log_tail,
                artifacts=[],
                elapsed_ms=result.elapsed_ms,
                backend=result.backend,
                working_dir=result.working_dir,
            )
            return self._finalize_result(job, result)

        result = JobResult(
            status="succeeded",
            phase="completed",
            exit_code=result.exit_code,
            error_kind=None,
            summary=result.summary,
            job_dir=result.job_dir,
            log_path=result.log_path,
            log_tail=result.log_tail,
            artifacts=artifacts,
            elapsed_ms=result.elapsed_ms,
            backend=result.backend,
            working_dir=result.working_dir,
        )
        return self._finalize_result(job, result)

    def _run_subprocess_job(self, job: _JobContext) -> JobResult:
        started_at = time.monotonic()
        executable = self._resolve_subprocess_executable()
        if executable is None:
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary="Unable to resolve a Stata executable from stata_path and edition.",
                job_dir=str(job.job_dir),
                log_path=None,
                log_tail="",
                artifacts=[],
                elapsed_ms=0,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )

        command = self._build_subprocess_command(executable, job.wrapper_do_path)
        try:
            completed = subprocess.run(
                command,
                cwd=job.working_dir,
                env=job.env,
                capture_output=True,
                text=True,
                timeout=job.timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            log_text = self._read_text(job.log_path)
            if not log_text:
                log_text = self._compose_process_output(exc.stdout, exc.stderr)
            return JobResult(
                status="failed",
                phase="execute",
                exit_code=124,
                error_kind="timeout",
                summary=f"Execution timed out after {job.timeout_sec}s and the subprocess was terminated.",
                job_dir=str(job.job_dir),
                log_path=str(job.log_path) if job.log_path.exists() else None,
                log_tail=self._tail_text(log_text),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                backend=job.backend,
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
                log_path=None,
                log_tail="",
                artifacts=[],
                elapsed_ms=elapsed_ms,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        log_text = self._read_text(job.log_path)
        process_output = self._compose_process_output(completed.stdout, completed.stderr)
        combined_text = log_text or process_output
        exit_code = self._parse_exit_code(combined_text, fallback=completed.returncode)

        if completed.returncode != 0 and not log_text.strip():
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=completed.returncode or 1,
                error_kind="bootstrap_error",
                summary=self._build_bootstrap_summary(process_output),
                job_dir=str(job.job_dir),
                log_path=None,
                log_tail=self._tail_text(process_output),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )

        error_kind = None if exit_code == 0 else self._classify_execution_failure(combined_text, exit_code)
        summary = self._build_execution_summary(combined_text, exit_code)
        return JobResult(
            status="succeeded" if exit_code == 0 else "failed",
            phase="completed" if exit_code == 0 else "execute",
            exit_code=exit_code,
            error_kind=error_kind,
            summary=summary,
            job_dir=str(job.job_dir),
            log_path=str(job.log_path) if job.log_path.exists() else None,
            log_tail=self._tail_text(combined_text),
            artifacts=[],
            elapsed_ms=elapsed_ms,
            backend=job.backend,
            working_dir=str(job.working_dir),
        )

    def _run_pystata_job(self, job: _JobContext) -> JobResult:
        started_at = time.monotonic()
        bootstrap_error = self._ensure_pystata_ready()
        if bootstrap_error is not None:
            return JobResult(
                status="failed",
                phase="bootstrap",
                exit_code=1,
                error_kind="bootstrap_error",
                summary=bootstrap_error,
                job_dir=str(job.job_dir),
                log_path=None,
                log_tail="",
                artifacts=[],
                elapsed_ms=0,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )

        try:
            stata = import_module("pystata.stata")
            safe_log_path = job.log_path.as_posix()
            safe_working_dir = job.working_dir.as_posix()
            safe_input_path = job.input_do_path.as_posix()
            log_name = f"agent_{uuid.uuid4().hex[:8]}"
            stata.run("capture log close _all")
            stata.run(f'log using "{safe_log_path}", replace text name({log_name})')
            stata.run(f'cd "{safe_working_dir}"')
            stata.run(f'capture noisily do "{safe_input_path}"')
            stata.run('display "__AGENT_RC__=" _rc')
            stata.run(f"log close {log_name}")
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            log_text = self._read_text(job.log_path)
            return JobResult(
                status="failed",
                phase="execute",
                exit_code=self._parse_exit_code(log_text, fallback=1),
                error_kind="stata_runtime_error",
                summary=f"pystata execution failed: {exc}",
                job_dir=str(job.job_dir),
                log_path=str(job.log_path) if job.log_path.exists() else None,
                log_tail=self._tail_text(log_text),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )
        finally:
            try:
                stata = import_module("pystata.stata")
                stata.run("capture log close _all")
            except Exception:
                pass

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        log_text = self._read_text(job.log_path)
        exit_code = self._parse_exit_code(log_text, fallback=1)
        if elapsed_ms > job.timeout_sec * 1000:
            return JobResult(
                status="failed",
                phase="execute",
                exit_code=124,
                error_kind="timeout",
                summary=(
                    f"Execution exceeded timeout budget ({job.timeout_sec}s) under the non-preemptive "
                    "pystata backend."
                ),
                job_dir=str(job.job_dir),
                log_path=str(job.log_path) if job.log_path.exists() else None,
                log_tail=self._tail_text(log_text),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                backend=job.backend,
                working_dir=str(job.working_dir),
            )

        error_kind = None if exit_code == 0 else self._classify_execution_failure(log_text, exit_code)
        return JobResult(
            status="succeeded" if exit_code == 0 else "failed",
            phase="completed" if exit_code == 0 else "execute",
            exit_code=exit_code,
            error_kind=error_kind,
            summary=self._build_execution_summary(log_text, exit_code),
            job_dir=str(job.job_dir),
            log_path=str(job.log_path) if job.log_path.exists() else None,
            log_tail=self._tail_text(log_text),
            artifacts=[],
            elapsed_ms=elapsed_ms,
            backend=job.backend,
            working_dir=str(job.working_dir),
        )

    def _ensure_pystata_ready(self) -> str | None:
        if self._pystata_ready:
            return None

        try:
            if self.config.stata_path:
                install_dir = self._resolve_stata_install_dir(Path(self.config.stata_path))
                self._bootstrap_pystata(install_dir)
            pystata_config = import_module("pystata.config")
            pystata_config.init(self.config.edition)
            self._pystata_ready = True
            return None
        except (FileNotFoundError, OSError) as exc:
            return (
                "Stata path is invalid. Set StataConfig.stata_path to a Stata executable or install "
                f"directory. Details: {exc}"
            )
        except Exception as exc:  # pragma: no cover - depends on local Stata setup
            return f"Failed to initialize pystata backend: {exc}"

    def _bootstrap_pystata(self, install_dir: Path) -> None:
        utilities_dir = (install_dir / "utilities").resolve()
        if not utilities_dir.is_dir():
            raise OSError(f"{install_dir.as_posix()} does not contain utilities/")

        utilities_str = str(utilities_dir)
        if utilities_str not in sys.path:
            sys.path.append(utilities_str)

    def _resolve_stata_install_dir(self, path: Path) -> Path:
        candidate = path.expanduser().resolve()
        if candidate.is_file():
            candidate = candidate.parent

        if (candidate / "utilities").is_dir():
            return candidate

        for child in candidate.glob("*"):
            if child.is_dir() and (child / "utilities").is_dir():
                return child

        raise OSError(f"{candidate.as_posix()} is not a valid Stata installation directory")

    def _resolve_subprocess_executable(self) -> Path | None:
        if not self.config.stata_path:
            return None

        path = Path(self.config.stata_path).expanduser()
        if path.exists() and path.is_file():
            return path.resolve()

        if path.exists() and path.is_dir():
            for name in _WINDOWS_EXECUTABLES[self.config.edition]:
                candidate = path / name
                if candidate.exists():
                    return candidate.resolve()
            return None

        return None

    def _build_subprocess_command(self, executable: Path, wrapper_do_path: Path) -> list[str]:
        if os.name == "nt":
            return [str(executable), "/e", "do", str(wrapper_do_path)]
        return [str(executable), "-b", "do", str(wrapper_do_path)]

    def _write_wrapper_do(self, job: _JobContext) -> None:
        safe_log_path = job.log_path.as_posix()
        safe_working_dir = job.working_dir.as_posix()
        safe_input_path = job.input_do_path.as_posix()
        wrapper = "\n".join(
            [
                "version 17.0",
                "clear all",
                "set more off",
                "capture log close _all",
                f'log using "{safe_log_path}", replace text name(agentlog)',
                f'cd "{safe_working_dir}"',
                f'capture noisily do "{safe_input_path}"',
                "local agent_rc = _rc",
                'display "__AGENT_RC__=`agent_rc\'"',
                "capture log close agentlog",
                "exit `agent_rc'",
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
            return "Stata job completed successfully."

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
                return f"Stata failed with exit_code={exit_code}: {line}"

        generic = [line.strip() for line in text.splitlines() if line.strip().startswith("r(")]
        if generic:
            return f"Stata failed with exit_code={exit_code}: {generic[-1]}"
        return f"Stata failed with exit_code={exit_code}."

    def _build_bootstrap_summary(self, text: str) -> str:
        stripped = [line.strip() for line in text.splitlines() if line.strip()]
        if stripped:
            return f"Stata subprocess bootstrap failed: {stripped[-1]}"
        return "Stata subprocess bootstrap failed before a log file was created."

    def _compose_process_output(self, stdout: str | None, stderr: str | None) -> str:
        parts = [chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()]
        return "\n".join(parts)

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
