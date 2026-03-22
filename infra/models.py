from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
from typing import Literal

from .config import StataConfig


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
    """Serializable job manifest that only describes execution facts."""

    status: JobStatus
    phase: JobPhase
    exit_code: int
    error_kind: ErrorKind | None
    summary: str
    job_dir: str
    run_log_path: str | None
    process_log_path: str | None
    log_tail: str
    artifacts: list[str]
    elapsed_ms: int
    working_dir: str
    diagnostic_excerpt: str = ""
    error_signature: str | None = None
    failed_command: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
