from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Mapping

from ..contract import Edition, ExecutorDefaults


_VALID_EDITIONS = {"mp", "se", "be"}
_ROOT_KEYS = {"stata_executable", "edition", "defaults"}
_DEFAULT_KEYS = {"timeout_sec", "artifact_globs"}


class UserConfigError(ValueError):
    """Raised when the user config file is missing required shape or contains invalid values."""


@dataclass(frozen=True, slots=True)
class UserConfig:
    config_path: Path
    exists: bool
    stata_executable: str | None
    edition: Edition
    defaults: ExecutorDefaults


def default_config_path(
    *,
    platform_name: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    effective_platform = platform_name or os.sys.platform
    effective_env = env or os.environ
    effective_home = home or Path.home()

    if effective_platform.startswith("win"):
        base = Path(effective_env.get("APPDATA", effective_home / "AppData" / "Roaming"))
    elif effective_platform == "darwin":
        base = effective_home / "Library" / "Application Support"
    else:
        base = Path(effective_env.get("XDG_CONFIG_HOME", effective_home / ".config"))

    return base / "stata-executor" / "config.json"


def load_user_config(
    *,
    config_path: Path | None = None,
    platform_name: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> UserConfig:
    resolved_path = config_path or default_config_path(
        platform_name=platform_name,
        env=env,
        home=home,
    )
    if not resolved_path.exists():
        return UserConfig(
            config_path=resolved_path,
            exists=False,
            stata_executable=None,
            edition="mp",
            defaults=ExecutorDefaults(),
        )

    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UserConfigError(f"Invalid JSON in config file: {resolved_path}") from exc
    if not isinstance(payload, dict):
        raise UserConfigError("Config root must be a JSON object.")

    unknown_root = sorted(set(payload) - _ROOT_KEYS)
    if unknown_root:
        raise UserConfigError(f"Unsupported config keys: {', '.join(unknown_root)}")

    stata_executable = payload.get("stata_executable")
    if stata_executable is not None and (not isinstance(stata_executable, str) or not stata_executable.strip()):
        raise UserConfigError("'stata_executable' must be a non-empty string when provided.")

    edition_raw = payload.get("edition", "mp")
    if not isinstance(edition_raw, str) or edition_raw not in _VALID_EDITIONS:
        raise UserConfigError("'edition' must be one of: mp, se, be.")

    defaults_payload = payload.get("defaults", {})
    if not isinstance(defaults_payload, dict):
        raise UserConfigError("'defaults' must be a JSON object when provided.")

    unknown_defaults = sorted(set(defaults_payload) - _DEFAULT_KEYS)
    if unknown_defaults:
        raise UserConfigError(f"Unsupported defaults keys: {', '.join(unknown_defaults)}")

    timeout_sec = defaults_payload.get("timeout_sec", 120)
    if not isinstance(timeout_sec, int) or timeout_sec <= 0:
        raise UserConfigError("'defaults.timeout_sec' must be a positive integer.")

    artifact_globs = defaults_payload.get("artifact_globs", [])
    if not isinstance(artifact_globs, list) or any(not isinstance(item, str) for item in artifact_globs):
        raise UserConfigError("'defaults.artifact_globs' must be an array of strings.")

    return UserConfig(
        config_path=resolved_path,
        exists=True,
        stata_executable=stata_executable.strip() if isinstance(stata_executable, str) else None,
        edition=edition_raw,
        defaults=ExecutorDefaults(timeout_sec=timeout_sec, artifact_globs=tuple(artifact_globs)),
    )
