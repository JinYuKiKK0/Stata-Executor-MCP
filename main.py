from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from infra import JobSpec, StataConfig, StataJobRunner


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--edition",
        type=str,
        default="mp",
        choices=["mp", "se", "be"],
        help="Stata edition used to resolve the executable.",
    )
    parser.add_argument(
        "--stata-path",
        type=str,
        default=None,
        help="Path to the Stata executable or installation directory.",
    )
    parser.add_argument(
        "--working-dir",
        type=str,
        default=None,
        help="Base working directory for resolving relative inputs and outputs.",
    )
    parser.add_argument(
        "--job-root",
        type=str,
        default=None,
        help="Directory where per-job manifests, logs, and input copies are stored.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="Hard timeout in seconds for one Stata job.",
    )
    parser.add_argument(
        "--artifact-glob",
        action="append",
        default=[],
        help="Relative glob used to collect new or changed artifacts after execution.",
    )
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment override in KEY=VALUE form. Can be repeated.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print compact JSON instead of pretty JSON.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stata job runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_do = subparsers.add_parser("run-do", help="Run a .do file as an isolated job")
    _add_common_arguments(run_do)
    run_do.add_argument("script", type=str, help="Path to the .do script, relative to working_dir if needed")

    run_inline = subparsers.add_parser("run-inline", help="Run inline commands as an isolated job")
    _add_common_arguments(run_inline)
    run_inline.add_argument(
        "commands",
        nargs="?",
        type=str,
        help="Inline commands. If omitted, commands are read from stdin.",
    )
    return parser


def _parse_env_overrides(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"Invalid --env value: {raw!r}. Expected KEY=VALUE.")
        key, value = raw.split("=", 1)
        if not key:
            raise ValueError("Environment override key cannot be empty.")
        env[key] = value
    return env


def _emit_result(result_json: str, compact: bool) -> None:
    if compact:
        print(result_json)
        return
    parsed = json.loads(result_json)
    print(json.dumps(parsed, ensure_ascii=False, indent=2))


def main() -> None:
    args = build_parser().parse_args()
    env_overrides = _parse_env_overrides(args.env)
    working_dir = Path(args.working_dir) if args.working_dir else Path.cwd()
    job_root = Path(args.job_root) if args.job_root else Path("logs/jobs")
    artifact_globs = tuple(args.artifact_glob)

    config = StataConfig(
        edition=args.edition,
        stata_path=args.stata_path,
        working_dir=working_dir,
        job_root=job_root,
        artifact_globs=artifact_globs,
        env_overrides=env_overrides,
    )
    spec = JobSpec(
        working_dir=working_dir,
        timeout_sec=args.timeout_sec,
        artifact_globs=artifact_globs,
        env_overrides=env_overrides,
    )

    runner = StataJobRunner(config)
    if args.command == "run-do":
        result = runner.run_do(args.script, spec)
    else:
        commands = args.commands if args.commands is not None else sys.stdin.read()
        result = runner.run_inline(commands, spec)

    _emit_result(result.to_json(), compact=args.json)
    sys.exit(0 if result.status == "succeeded" else (result.exit_code or 1))


if __name__ == "__main__":
    main()
