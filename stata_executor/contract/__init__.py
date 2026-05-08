from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

Edition = Literal["mp", "se", "be"]
ExecutionStatus = Literal["succeeded", "failed"]
ErrorKind = Literal[
    "bootstrap_error",
    "input_error",
    "timeout",
    "stata_parse_or_command_error",
    "stata_runtime_error",
    "artifact_collection_error",
]
ConfigSource = Literal["explicit", "env", "missing"]


@dataclass(frozen=True, slots=True)
class ExecutorDefaults:
    timeout_sec: int = 120
    artifact_globs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RunDoRequest:
    script_path: str
    working_dir: str | None = None
    timeout_sec: int | None = None
    artifact_globs: tuple[str, ...] = ()
    edition: Edition | None = None
    stata_executable: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunInlineRequest:
    commands: str
    working_dir: str | None = None
    timeout_sec: int | None = None
    artifact_globs: tuple[str, ...] = ()
    edition: Edition | None = None
    stata_executable: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: ExecutionStatus
    error_kind: ErrorKind | None
    result_text: str
    diagnostic_excerpt: str
    artifacts: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, *, pretty: bool = False) -> str:
        kwargs = {"ensure_ascii": False}
        if pretty:
            kwargs["indent"] = 2
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class DoctorResult:
    ready: bool
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, *, pretty: bool = False) -> str:
        kwargs = {"ensure_ascii": False}
        if pretty:
            kwargs["indent"] = 2
        return json.dumps(self.to_dict(), **kwargs)
