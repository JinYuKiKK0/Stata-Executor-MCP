from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import re
import time
import uuid
from importlib import import_module
from pathlib import Path
import sys
from typing import Literal

from .config import StataConfig


class StataEngineError(Exception):
    """Base exception for Stata engine errors."""


class StataNotInstalledError(StataEngineError):
    """Stata is not installed or not accessible."""


class StataLicenseError(StataEngineError):
    """Stata is installed but license is invalid or unavailable."""


class StataCommandError(StataEngineError):
    """Stata command failed."""


ErrorType = Literal[
    "engine_init_error",
    "license_error",
    "file_error",
    "timeout",
    "stata_error",
    "command_or_syntax_error",
]


@dataclass(slots=True)
class ExecutionResult:
    """Structured execution result contract for agent consumption."""

    ok: bool
    rc: int
    error_type: ErrorType | None
    summary: str
    log_path: str | None
    log_tail: str
    artifacts: list[str]
    elapsed_ms: int
    working_dir: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class StataEngine:
    """Minimal local execution engine backed by pystata."""

    def __init__(self, config: StataConfig):
        self.config = config
        self.working_dir = config.resolve_working_dir()
        self.log_dir = config.resolve_log_dir()
        self._latest_result: ExecutionResult | None = None
        self._init_stata()

    def _init_stata(self) -> None:
        try:
            if self.config.stata_path:
                install_dir = self._resolve_stata_install_dir(Path(self.config.stata_path))
                self._bootstrap_pystata(install_dir)
                pystata_config = import_module("pystata.config")
                pystata_config.init(self.config.edition)
            else:
                pystata_config = import_module("pystata.config")

                pystata_config.init(self.config.edition)

            stata = import_module("pystata.stata")

            stata.run("clear all")
        except (FileNotFoundError, OSError) as exc:
            raise StataNotInstalledError(
                "Stata path is invalid. Set StataConfig.stata_path to your Stata install directory that contains 'utilities'."
            ) from exc
        except Exception as exc:  # pragma: no cover - depends on local Stata setup
            msg = str(exc).lower()
            if "license" in msg:
                raise StataLicenseError(str(exc)) from exc
            raise StataEngineError(f"Failed to initialize pystata: {exc}") from exc

    def _bootstrap_pystata(self, install_dir: Path) -> None:
        """Add Stata utilities folder into sys.path so pystata can be imported."""
        utilities_dir = (install_dir / "utilities").resolve()
        if not utilities_dir.is_dir():
            raise OSError(f"{install_dir.as_posix()} does not contain utilities/")

        utilities_str = str(utilities_dir)
        if utilities_str not in sys.path:
            sys.path.append(utilities_str)

    def _resolve_stata_install_dir(self, path: Path) -> Path:
        """Resolve a Stata installation directory accepted by stata_setup.config."""
        candidate = path.expanduser().resolve()

        if candidate.is_file():
            candidate = candidate.parent

        if (candidate / "utilities").is_dir():
            return candidate

        for child in candidate.glob("*"):
            if child.is_dir() and (child / "utilities").is_dir():
                return child

        raise OSError(f"{candidate.as_posix()} is not a valid Stata installation directory")

    def run(self, command: str, timeout_sec: int | None = None) -> ExecutionResult:
        """Execute one command or command block and return a structured result."""
        return self._run_with_log(command=command, timeout_sec=timeout_sec)

    def run_script(self, script_path: str | Path, timeout_sec: int | None = None) -> ExecutionResult:
        script = Path(script_path).resolve()
        if not script.exists():
            return self._file_error_result(f"Script does not exist: {script}")
        safe_path = script.as_posix()
        return self._run_with_log(command=f'do "{safe_path}"', timeout_sec=timeout_sec)

    def load_data(self, file_path: str | Path, timeout_sec: int | None = None) -> ExecutionResult:
        path = Path(file_path).resolve()
        if not path.exists():
            return self._file_error_result(f"Data file does not exist: {path}")

        safe_path = path.as_posix()
        suffix = path.suffix.lower()
        if suffix == ".dta":
            cmd = f'use "{safe_path}", clear'
        elif suffix == ".csv":
            cmd = f'import delimited using "{safe_path}", clear'
        else:
            return self._file_error_result(f"Unsupported data format: {path.suffix}")
        return self._run_with_log(command=cmd, timeout_sec=timeout_sec)

    def export_data(self, file_path: str | Path, timeout_sec: int | None = None) -> ExecutionResult:
        path = Path(file_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_path = path.as_posix()
        suffix = path.suffix.lower()
        if suffix == ".dta":
            cmd = f'save "{safe_path}", replace'
        elif suffix == ".csv":
            cmd = f'export delimited using "{safe_path}", replace'
        else:
            return self._file_error_result(f"Unsupported export format: {path.suffix}")
        return self._run_with_log(command=cmd, timeout_sec=timeout_sec)

    def get_output(self) -> str:
        if self._latest_result is None:
            return ""
        return self._latest_result.log_tail

    def get_latest_result(self) -> ExecutionResult | None:
        return self._latest_result

    def close(self) -> None:
        try:
            stata = import_module("pystata.stata")

            stata.run("capture log close _all")
        except Exception:
            pass

    def _run_with_log(self, command: str, timeout_sec: int | None = None) -> ExecutionResult:
        started_at = time.monotonic()
        effective_timeout = timeout_sec or self.config.default_timeout_sec
        log_file = self.log_dir / f"stata_{int(time.time())}_{uuid.uuid4().hex[:8]}.log"
        log_name = f"agent_{uuid.uuid4().hex[:8]}"

        try:
            stata = import_module("pystata.stata")
            safe_log_path = log_file.as_posix()
            safe_working_dir = self.working_dir.as_posix()

            stata.run("capture log close _all")
            stata.run(f'log using "{safe_log_path}", replace text name({log_name})')
            stata.run(f'cd "{safe_working_dir}"')
            # Capture rc from target command so caller can branch without parsing full logs.
            stata.run(f"capture noisily {command}")
            stata.run('display "__AGENT_RC__=" _rc')
            stata.run(f"log close {log_name}")

            output = log_file.read_text(encoding="utf-8", errors="ignore")
            rc = self._parse_rc(output)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            if elapsed_ms > int(effective_timeout * 1000):
                result = ExecutionResult(
                    ok=False,
                    rc=rc,
                    error_type="timeout",
                    summary=(
                        f"Execution exceeded timeout budget ({effective_timeout}s) "
                        "under non-preemptive pystata backend."
                    ),
                    log_path=str(log_file),
                    log_tail=self._tail_log(output),
                    artifacts=[],
                    elapsed_ms=elapsed_ms,
                    working_dir=str(self.working_dir),
                )
                self._latest_result = result
                return result

            ok = rc == 0
            error_type = None if ok else self._classify_error(output, rc)
            result = ExecutionResult(
                ok=ok,
                rc=rc,
                error_type=error_type,
                summary=self._build_summary(ok=ok, rc=rc, output=output),
                log_path=str(log_file),
                log_tail=self._tail_log(output),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                working_dir=str(self.working_dir),
            )
            self._latest_result = result
            return result
        except Exception as exc:  # pragma: no cover - depends on runtime and Stata
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            log_text = ""
            if log_file.exists():
                log_text = log_file.read_text(encoding="utf-8", errors="ignore")
            result = ExecutionResult(
                ok=False,
                rc=self._parse_rc(log_text, fallback=1),
                error_type="stata_error",
                summary=f"Failed to run Stata command: {exc}",
                log_path=str(log_file) if log_file.exists() else None,
                log_tail=self._tail_log(log_text),
                artifacts=[],
                elapsed_ms=elapsed_ms,
                working_dir=str(self.working_dir),
            )
            self._latest_result = result
            return result
        finally:
            try:
                stata = import_module("pystata.stata")
                stata.run("capture log close _all")
            except Exception:
                pass

    def _file_error_result(self, message: str) -> ExecutionResult:
        result = ExecutionResult(
            ok=False,
            rc=601,
            error_type="file_error",
            summary=message,
            log_path=None,
            log_tail="",
            artifacts=[],
            elapsed_ms=0,
            working_dir=str(self.working_dir),
        )
        self._latest_result = result
        return result

    def _parse_rc(self, output: str, fallback: int = 1) -> int:
        matches = re.findall(r"__AGENT_RC__\s*=\s*(\d+)", output)
        if matches:
            return int(matches[-1])
        # Last-resort extraction for logs that only contain `r(<code>)`.
        generic = re.findall(r"r\((\d+)\)", output)
        if generic:
            return int(generic[-1])
        return fallback

    def _tail_log(self, output: str, lines: int = 40) -> str:
        if not output:
            return ""
        split = output.splitlines()
        return "\n".join(split[-lines:])

    def _classify_error(self, output: str, rc: int) -> ErrorType:
        low = output.lower()
        if rc == 199:
            return "command_or_syntax_error"
        if "invalid syntax" in low or "unrecognized command" in low:
            return "command_or_syntax_error"
        if "license" in low:
            return "license_error"
        return "stata_error"

    def _build_summary(self, ok: bool, rc: int, output: str) -> str:
        if ok:
            return "Stata execution completed successfully."

        candidates = [
            line.strip()
            for line in output.splitlines()
            if line.strip() and (line.strip().startswith("r(") or "error" in line.lower())
        ]
        if candidates:
            return f"Stata failed with rc={rc}: {candidates[-1]}"
        return f"Stata failed with rc={rc}."
