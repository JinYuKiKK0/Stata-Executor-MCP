from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(slots=True)
class StataConfig:
    """Static runner configuration shared by all Stata jobs."""

    edition: Literal["mp", "se", "be"] = "mp"
    stata_path: str | None = None
    working_dir: Path = Path.cwd()
    job_root: Path = Path("logs/jobs")
    default_timeout_sec: int = 120
    artifact_globs: tuple[str, ...] = ()
    env_overrides: dict[str, str] = field(default_factory=dict)

    def resolve_working_dir(self) -> Path:
        path = Path(self.working_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_job_root(self) -> Path:
        base = Path(self.job_root)
        if not base.is_absolute():
            base = self.resolve_working_dir() / base
        path = base.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
