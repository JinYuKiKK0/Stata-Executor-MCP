from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


DEFAULT_REPO_ROOT = Path(r"D:\Developments\PythonProject\StataAgent")


class WrapperArgumentError(Exception):
    """Raised when wrapper CLI arguments cannot be parsed into a valid request."""


class WrapperBootstrapError(Exception):
    """Raised when the wrapper cannot bootstrap the StataAgent CLI."""


class StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WrapperArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = StableArgumentParser(description="Run Stata jobs through the local StataAgent CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_do = subparsers.add_parser("run-do", help="Run an existing .do file")
    add_common_arguments(run_do)
    run_do.add_argument("script", help="Path to the .do file")

    run_inline = subparsers.add_parser("run-inline", help="Run inline Stata commands")
    add_common_arguments(run_inline)
    run_inline.add_argument(
        "commands",
        nargs="?",
        help="Inline Stata commands. If omitted, commands are read from stdin.",
    )
    return parser


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", help="Path to the StataAgent repository")
    parser.add_argument("--uv-path", help="Path to a working uv executable")
    parser.add_argument("--edition", default="mp", choices=("mp", "se", "be"))
    parser.add_argument(
        "--stata-path",
        required=True,
        help="Path to the Stata executable or install directory",
    )
    parser.add_argument("--working-dir", help="Base working directory for relative inputs and outputs")
    parser.add_argument("--job-root", help="Directory where job manifests and logs are written")
    parser.add_argument("--timeout-sec", type=int, help="Hard timeout in seconds")
    parser.add_argument(
        "--artifact-glob",
        action="append",
        default=[],
        help="Repeatable artifact glob relative to the working directory",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Repeatable environment override in KEY=VALUE form",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Print compact JSON instead of pretty JSON",
    )


def main() -> int:
    parsed_working_dir: str | None = None
    try:
        args = build_parser().parse_args()
        parsed_working_dir = args.working_dir
        repo_root = resolve_repo_root(args.repo_root)
        runner_prefix = resolve_runner_prefix(repo_root, args.uv_path)
        stdin_text, command = build_agent_command(args, repo_root, runner_prefix)
        completed = subprocess.run(
            command,
            cwd=repo_root,
            input=stdin_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.stderr.strip():
            print(completed.stderr, file=sys.stderr, end="")

        payload = parse_cli_payload(completed.stdout, completed.returncode, completed.stderr)
        emit_result(payload, compact=args.compact)
        return completed.returncode
    except WrapperArgumentError as exc:
        emit_wrapper_error(
            summary=f"Wrapper argument error: {exc}",
            phase="input",
            error_kind="input_error",
            exit_code=2,
            working_dir=parsed_working_dir,
        )
        return 2
    except WrapperBootstrapError as exc:
        emit_wrapper_error(str(exc), working_dir=parsed_working_dir)
        return 1


def build_agent_command(
    args: argparse.Namespace,
    repo_root: Path,
    runner_prefix: list[str],
) -> tuple[str | None, list[str]]:
    stdin_text = None
    command = [*runner_prefix, "main.py", args.command]

    if args.command == "run-do":
        command.append(args.script)
    else:
        if args.commands is None:
            stdin_text = sys.stdin.read()
            if not stdin_text.strip():
                raise WrapperArgumentError("run-inline requires commands or stdin input.")
        else:
            command.append(args.commands)

    append_optional_flag(command, "--edition", args.edition)
    append_optional_flag(command, "--stata-path", args.stata_path)
    append_optional_flag(command, "--working-dir", args.working_dir)
    append_optional_flag(command, "--job-root", args.job_root)
    append_optional_flag(command, "--timeout-sec", args.timeout_sec)
    for glob in args.artifact_glob:
        command.extend(["--artifact-glob", glob])
    for override in args.env:
        command.extend(["--env", override])
    command.append("--json")

    if not (repo_root / "main.py").is_file():
        raise WrapperBootstrapError(f"Resolved repo root does not contain main.py: {repo_root}")
    return stdin_text, command


def parse_cli_payload(stdout: str, returncode: int, stderr: str) -> dict[str, object]:
    stripped = stdout.strip()
    if not stripped:
        excerpt = stderr.strip()
        raise WrapperBootstrapError(
            f"StataAgent CLI returned no JSON output. Exit code={returncode}."
            + (f" Stderr: {excerpt}" if excerpt else "")
        )

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        excerpt = (stderr.strip() or stripped).strip()
        raise WrapperBootstrapError(
            f"StataAgent CLI returned non-JSON output. Exit code={returncode}."
            + (f" Output: {excerpt}" if excerpt else "")
        ) from exc

    if not isinstance(payload, dict):
        raise WrapperBootstrapError(f"StataAgent CLI returned a non-object JSON payload. Exit code={returncode}.")
    return payload


def emit_result(payload: dict[str, object], compact: bool) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def emit_wrapper_error(
    summary: str,
    *,
    phase: str = "bootstrap",
    error_kind: str = "bootstrap_error",
    exit_code: int = 1,
    working_dir: str | None = None,
) -> None:
    payload = {
        "status": "failed",
        "phase": phase,
        "exit_code": exit_code,
        "error_kind": error_kind,
        "summary": summary,
        "job_dir": None,
        "run_log_path": None,
        "process_log_path": None,
        "log_tail": "",
        "artifacts": [],
        "elapsed_ms": 0,
        "working_dir": str(Path(working_dir).resolve() if working_dir else Path.cwd()),
        "diagnostic_excerpt": "",
        "error_signature": None,
        "failed_command": None,
    }
    print(json.dumps(payload, ensure_ascii=False))


def append_optional_flag(command: list[str], flag: str, value: object | None) -> None:
    if value is None:
        return
    command.extend([flag, str(value)])


def resolve_repo_root(explicit: str | None) -> Path:
    for candidate in iter_repo_root_candidates(explicit):
        if is_repo_root(candidate):
            return candidate.resolve()
    raise WrapperBootstrapError(
        "Cannot locate the StataAgent repository. Pass --repo-root or set STATA_AGENT_ROOT."
    )


def iter_repo_root_candidates(explicit: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())

    env_value = os.getenv("STATA_AGENT_ROOT")
    if env_value:
        candidates.append(Path(env_value).expanduser())

    cwd = Path.cwd()
    candidates.append(cwd)
    candidates.extend(cwd.parents)
    candidates.append(DEFAULT_REPO_ROOT)
    return dedupe_paths(candidates)


def resolve_runner_prefix(repo_root: Path, explicit_uv: str | None) -> list[str]:
    uv_executable = resolve_uv_executable(explicit_uv)
    if uv_executable is not None:
        return [str(uv_executable), "run", "python"]

    repo_python = resolve_repo_python(repo_root)
    if repo_python is not None:
        return [str(repo_python)]

    raise WrapperBootstrapError(
        "Cannot locate a working uv executable or repo-local .venv Python. "
        "Pass --uv-path, set UV_EXE, or create a working .venv under the repository root."
    )


def resolve_uv_executable(explicit: str | None) -> Path | None:
    for candidate in iter_uv_candidates(explicit):
        if candidate.is_file() and is_working_command([str(candidate), "--version"]):
            return candidate.resolve()
    return None


def iter_uv_candidates(explicit: str | None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())

    env_value = os.getenv("UV_EXE")
    if env_value:
        candidates.append(Path(env_value).expanduser())

    which_value = shutil.which("uv")
    if which_value:
        candidates.append(Path(which_value))

    winget_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        candidates.extend(sorted(winget_root.glob("astral-sh.uv*/uv.exe")))

    cargo_uv = Path.home() / ".cargo" / "bin" / "uv.exe"
    if cargo_uv.exists():
        candidates.append(cargo_uv)
    return dedupe_paths(candidates)


def resolve_repo_python(repo_root: Path) -> Path | None:
    candidates = [
        repo_root / ".venv" / "Scripts" / "python.exe",
        repo_root / ".venv" / "Scripts" / "python.cmd",
        repo_root / ".venv" / "Scripts" / "python.bat",
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python3",
    ]
    for candidate in candidates:
        if candidate.is_file() and is_working_command([str(candidate), "--version"]):
            return candidate.resolve()
    return None


def is_repo_root(path: Path) -> bool:
    return (path / "main.py").is_file() and (path / "infra" / "__init__.py").is_file()


def is_working_command(command: list[str]) -> bool:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


if __name__ == "__main__":
    raise SystemExit(main())
