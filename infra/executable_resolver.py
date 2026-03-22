from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows import guard
    winreg = None


_EDITION_PREFIX = {
    "mp": "statamp",
    "se": "statase",
    "be": "statabe",
}
_EDITION_ENV_VARS = {
    "mp": ("STATA_MP_PATH", "STATA_MP_EXE"),
    "se": ("STATA_SE_PATH", "STATA_SE_EXE"),
    "be": ("STATA_BE_PATH", "STATA_BE_EXE"),
}
_GENERIC_ENV_VARS = ("STATA_PATH", "STATA_EXE", "STATA_HOME")
_WINDOWS_PRODUCT_NAMES = {
    "mp": ("StataMP",),
    "se": ("StataSE",),
    "be": ("StataBE", "Stata"),
}
_WINDOWS_VERSIONS = ("18", "17")
_HEADLESS_HINTS = ("console", "batch", "automation", "headless")


def resolve_stata_executable(
    stata_path: str | None,
    edition: Literal["mp", "se", "be"],
) -> Path | None:
    """Resolve the best available Stata executable for subprocess execution."""

    if stata_path:
        return _resolve_candidate(Path(stata_path).expanduser(), edition)

    explicit_env_candidates = list(_iter_explicit_env_candidates(edition))
    if explicit_env_candidates:
        for candidate in explicit_env_candidates:
            resolved = _resolve_candidate(candidate, edition)
            if resolved is not None:
                return resolved
        return None

    for candidate in _iter_auto_discovery_candidates(edition):
        resolved = _resolve_candidate(candidate, edition)
        if resolved is not None:
            return resolved
    return None


def find_preferred_executable(
    directory: Path,
    edition: Literal["mp", "se", "be"],
) -> Path | None:
    if not directory.exists() or not directory.is_dir():
        return None

    prefix = _EDITION_PREFIX[edition]
    candidates = [path for path in directory.glob("*.exe") if path.stem.lower().startswith(prefix)]
    if not candidates:
        return None

    def score(candidate: Path) -> tuple[int, int, int, str]:
        name = candidate.name.lower()
        headless_rank = 0 if any(hint in name for hint in _HEADLESS_HINTS) else 1
        sixty_four_rank = 0 if "64" in name else 1
        gui_rank = 1 if name.startswith(prefix) else 2
        return (headless_rank, sixty_four_rank, gui_rank, name)

    candidates.sort(key=score)
    return candidates[0].resolve()


def build_stata_command(executable: Path, wrapper_do_path: Path) -> list[str]:
    if os.name == "nt":
        return [str(executable), "/q", "/i", "/e", "do", str(wrapper_do_path)]
    return [str(executable), "-b", "do", str(wrapper_do_path)]


def _resolve_candidate(
    path: Path,
    edition: Literal["mp", "se", "be"],
) -> Path | None:
    if path.exists() and path.is_file():
        preferred = find_preferred_executable(path.parent, edition)
        if preferred is not None:
            return preferred
        return path.resolve()

    if path.exists() and path.is_dir():
        return find_preferred_executable(path, edition)

    return None


def _iter_explicit_env_candidates(edition: Literal["mp", "se", "be"]) -> list[Path]:
    candidates: list[Path] = []
    for key in (*_EDITION_ENV_VARS[edition], *_GENERIC_ENV_VARS):
        value = os.getenv(key)
        if value:
            candidates.append(Path(value).expanduser())
    return _dedupe_paths(candidates)


def _iter_auto_discovery_candidates(edition: Literal["mp", "se", "be"]) -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.extend(_iter_windows_registry_candidates(edition))
        candidates.extend(_iter_common_windows_install_dirs())
    return _dedupe_paths(candidates)


def _iter_windows_registry_candidates(
    edition: Literal["mp", "se", "be"],
) -> list[Path]:
    if os.name != "nt" or winreg is None:
        return []

    candidates: list[Path] = []
    hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    prefixes = ("SOFTWARE", r"SOFTWARE\WOW6432Node")

    for hive in hives:
        for prefix in prefixes:
            for product in _WINDOWS_PRODUCT_NAMES[edition]:
                for version in _WINDOWS_VERSIONS:
                    key_path = f"{prefix}\\StataCorp\\{product}{version}"
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            for value_name in ("StataEXE", "InstallPath"):
                                try:
                                    value, _ = winreg.QueryValueEx(key, value_name)
                                except OSError:
                                    continue
                                if isinstance(value, str) and value.strip():
                                    candidates.append(Path(value).expanduser())
                    except OSError:
                        continue

    return _dedupe_paths(candidates)


def _iter_common_windows_install_dirs() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        value = os.getenv(env_name)
        if value:
            roots.append(Path(value).expanduser())

    candidates: list[Path] = []
    for root in _dedupe_paths(roots):
        for version in _WINDOWS_VERSIONS:
            candidates.append(root / f"Stata{version}")
    return _dedupe_paths(candidates)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique
