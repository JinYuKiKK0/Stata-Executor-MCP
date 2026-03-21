from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from infra import StataConfig, StataEngine, StataEngineError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local StataAgent MVP runner")
    parser.add_argument("--script", type=str, help="Optional .do file to run")
    parser.add_argument(
        "--edition",
        type=str,
        default="mp",
        choices=["mp", "se", "be"],
        help="Stata edition for pystata init",
    )
    parser.add_argument(
        "--stata-path",
        type=str,
        default=None,
        help="Optional Stata install path (overrides STATA_PATH env and config default)",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="Optional timeout budget in seconds for one execution job",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON result for machine consumption",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    default_config = StataConfig()
    resolved_stata_path = args.stata_path or os.getenv("STATA_PATH") or default_config.stata_path

    config = StataConfig(
        edition=args.edition,
        stata_path=resolved_stata_path,
        working_dir=Path.cwd(),
        log_dir=Path("logs"),
    )

    try:
        engine = StataEngine(config)

        if args.script:
            result = engine.run_script(args.script, timeout_sec=args.timeout_sec)
        else:
            # Minimal smoke test to verify local pystata wiring.
            result = engine.run(
                """
                sysuse auto, clear
                summarize price weight mpg
                regress price weight mpg
                """.strip(),
                timeout_sec=args.timeout_sec,
            )

        if args.json:
            print(result.to_json())
        else:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

        if not result.ok:
            sys.exit(result.rc if result.rc > 0 else 1)
    except StataEngineError as exc:
        failure = {
            "ok": False,
            "rc": 1,
            "error_type": "engine_init_error",
            "summary": f"StataEngine startup/execution failed: {exc}",
            "log_path": None,
            "log_tail": "",
            "artifacts": [],
            "elapsed_ms": 0,
            "working_dir": str(Path.cwd()),
        }
        if args.json:
            print(json.dumps(failure, ensure_ascii=False))
        else:
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            print(f"Effective stata_path: {config.stata_path}")
            print(
                "Hint: set --stata-path or STATA_PATH to a directory that contains the 'utilities' folder."
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
