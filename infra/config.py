from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class StataConfig:
    """Runtime settings for local Stata execution."""

    edition: str = "mp"
    stata_path: str | None = None
    working_dir: Path = Path.cwd()
    log_dir: Path = Path("logs")
    default_timeout_sec: int = 120

    def resolve_working_dir(self) -> Path:
        path = Path(self.working_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_log_dir(self) -> Path:
        base = Path(self.log_dir)
        if not base.is_absolute():
            base = self.resolve_working_dir() / base
        path = base.resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
