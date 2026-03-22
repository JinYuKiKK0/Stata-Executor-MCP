from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import unittest
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from stata_executor import RunDoRequest, RunInlineRequest, StataExecutor
from stata_executor.config import default_config_path, load_user_config
from stata_executor.runtime.executable_resolver import build_stata_command, resolve_stata_executable


class StataExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._case_dirs: list[Path] = []

    def tearDown(self) -> None:
        if os.getenv("KEEP_TEST_ARTIFACTS", "0") == "1":
            return
        for case_dir in self._case_dirs:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_default_config_path_matches_platform_rules(self) -> None:
        home = Path("C:/Users/Test")
        win_path = default_config_path(platform_name="win32", env={"APPDATA": "C:/Users/Test/AppData/Roaming"}, home=home)
        mac_path = default_config_path(platform_name="darwin", env={}, home=Path("/Users/test"))
        linux_path = default_config_path(platform_name="linux", env={}, home=Path("/home/test"))

        self.assertEqual(win_path, Path("C:/Users/Test/AppData/Roaming/stata-executor/config.json"))
        self.assertEqual(mac_path, Path("/Users/test/Library/Application Support/stata-executor/config.json"))
        self.assertEqual(linux_path, Path("/home/test/.config/stata-executor/config.json"))

    def test_load_user_config_rejects_invalid_json(self) -> None:
        root = self._workspace_case_dir()
        config_path = root / "config.json"
        config_path.write_text("{not-json", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Invalid JSON"):
            load_user_config(config_path=config_path)

    def test_doctor_reports_missing_configuration(self) -> None:
        root = self._workspace_case_dir()
        config_home = root / "appdata"
        config_home.mkdir(parents=True, exist_ok=True)
        previous = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(config_home)
        try:
            result = StataExecutor().doctor()
        finally:
            if previous is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = previous

        self.assertFalse(result.ready)
        self.assertEqual(result.config_source, "missing")
        self.assertIn("No Stata executable configured", result.summary)

    def test_doctor_uses_user_config_and_resolves_executable(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        config_root = root / "appdata" / "stata-executor"
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / "config.json").write_text(
            json.dumps(
                {
                    "stata_executable": str(fake_exe),
                    "edition": "mp",
                    "defaults": {
                        "timeout_sec": 90,
                        "artifact_globs": ["reports/**/*.rtf"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        previous = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(root / "appdata")
        try:
            result = StataExecutor().doctor()
        finally:
            if previous is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = previous

        self.assertTrue(result.ready)
        self.assertEqual(result.config_source, "user_config")
        self.assertTrue(result.stata_executable.endswith("fake_stata.cmd"))
        self.assertEqual(result.defaults.timeout_sec, 90)

    def test_executable_resolution_prefers_headless_candidate(self) -> None:
        root = self._workspace_case_dir()
        install_dir = root / "stata17"
        install_dir.mkdir(parents=True, exist_ok=True)
        gui = install_dir / "StataMP-64.exe"
        headless = install_dir / "StataMP-console.exe"
        gui.write_text("", encoding="utf-8")
        headless.write_text("", encoding="utf-8")

        resolved = resolve_stata_executable(str(install_dir), "mp")

        self.assertEqual(resolved, headless.resolve())

    def test_wrapper_command_keeps_windows_flags(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        wrapper = root / "wrapper.do"
        wrapper.write_text("", encoding="utf-8")

        command = build_stata_command(fake_exe, wrapper)

        if sys.platform.startswith("win"):
            self.assertEqual(command[1:4], ["/q", "/i", "/e"])

    def test_missing_script_returns_input_error(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)

        result = StataExecutor().run_do(
            RunDoRequest(
                script_path="missing.do",
                working_dir=str(root / "wd"),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.phase, "input")
        self.assertEqual(result.error_kind, "input_error")
        self.assertIsNotNone(result.job_dir)
        self.assertTrue((Path(result.job_dir) / "result.json").exists())

    def test_run_inline_reports_parse_error(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)

        result = StataExecutor().run_inline(
            RunInlineRequest(
                commands="FAKE_ERROR 199|command foo is unrecognized",
                working_dir=str(root / "wd"),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_kind, "stata_parse_or_command_error")
        self.assertIn("command foo is unrecognized", result.summary)

    def test_failed_job_returns_mechanical_diagnostics(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)

        result = StataExecutor().run_inline(
            RunInlineRequest(
                commands="regress price weight mpg\nFAKE_ERROR 111|variable mpg not found",
                working_dir=str(root / "wd"),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(result.error_signature, "variable mpg not found")
        self.assertEqual(result.failed_command, "regress price weight mpg")
        self.assertIn(". regress price weight mpg", result.diagnostic_excerpt)
        self.assertNotIn("__AGENT_RC__", result.diagnostic_excerpt)

    def test_success_job_collects_artifacts(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        working_dir = root / "wd"

        result = StataExecutor().run_inline(
            RunInlineRequest(
                commands="FAKE_WRITE output/result.txt",
                working_dir=str(working_dir),
                artifact_globs=("output/**/*.txt",),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, "completed")
        self.assertEqual(result.artifacts, [str((working_dir / "output" / "result.txt").resolve())])
        self.assertTrue((working_dir / ".stata-executor" / "jobs" / result.job_id / "result.json").exists())

    def test_failed_job_still_collects_artifacts(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        working_dir = root / "wd"

        result = StataExecutor().run_inline(
            RunInlineRequest(
                commands="FAKE_WRITE reports/partial.txt\nFAKE_ERROR 199|command foo is unrecognized",
                working_dir=str(working_dir),
                artifact_globs=("reports/**/*.txt",),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.artifacts, [str((working_dir / "reports" / "partial.txt").resolve())])

    def test_timeout_terminates_subprocess_and_next_job_is_clean(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        executor = StataExecutor()

        timed_out = executor.run_inline(
            RunInlineRequest(
                commands="FAKE_SLEEP 2",
                working_dir=str(root / "wd"),
                timeout_sec=1,
                stata_executable=str(fake_exe),
            )
        )
        succeeded = executor.run_inline(
            RunInlineRequest(
                commands="FAKE_WRITE reports/ok.txt",
                working_dir=str(root / "wd"),
                timeout_sec=5,
                artifact_globs=("reports/**/*.txt",),
                stata_executable=str(fake_exe),
            )
        )

        self.assertEqual(timed_out.error_kind, "timeout")
        self.assertEqual(succeeded.status, "succeeded")
        self.assertNotEqual(timed_out.job_id, succeeded.job_id)

    def test_cli_doctor_returns_json_and_exit_code(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "stata_executor", "doctor"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.stderr.strip(), "")
        payload = json.loads(completed.stdout)
        self.assertIn("ready", payload)
        self.assertIn(completed.returncode, {0, 1})

    def test_cli_argument_errors_return_stable_json(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "stata_executor", "run-inline", "--env", "INVALID"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["phase"], "input")
        self.assertEqual(payload["error_kind"], "input_error")

    def test_mcp_server_lists_tools_and_runs_inline(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        working_dir = root / "wd"
        process = subprocess.Popen(
            [sys.executable, "-m", "stata_executor.adapters.mcp"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._send_mcp(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
            init_response = self._read_mcp(process)
            self.assertEqual(init_response["result"]["serverInfo"]["name"], "stata-executor")

            self._send_mcp(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._send_mcp(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            list_response = self._read_mcp(process)
            names = {tool["name"] for tool in list_response["result"]["tools"]}
            self.assertEqual(names, {"doctor", "run_do", "run_inline"})

            self._send_mcp(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "run_inline",
                        "arguments": {
                            "commands": "FAKE_WRITE output/result.txt",
                            "working_dir": str(working_dir),
                            "artifact_globs": ["output/**/*.txt"],
                            "stata_executable": str(fake_exe),
                        },
                    },
                },
            )
            run_response = self._read_mcp(process)
            self.assertFalse(run_response["result"]["isError"])
            self.assertEqual(
                run_response["result"]["structuredContent"]["artifacts"],
                [str((working_dir / "output" / "result.txt").resolve())],
            )
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            process.terminate()
            process.wait(timeout=5)

    def _create_fake_stata_executable(self, root: Path) -> Path:
        fake_py = root / "fake_stata.py"
        fake_py.write_text(
            textwrap.dedent(
                """
                from __future__ import annotations

                import re
                from pathlib import Path
                import sys
                import time


                def parse_wrapper(path: Path) -> tuple[Path, Path, Path]:
                    text = path.read_text(encoding="utf-8")
                    run_log_match = re.search(r'log using "([^"]+)"', text)
                    cwd_match = re.search(r'cd "([^"]+)"', text)
                    do_match = re.search(r'do "([^"]+)"', text)
                    if not run_log_match or not cwd_match or not do_match:
                        raise RuntimeError("wrapper format changed")
                    return Path(run_log_match.group(1)), Path(cwd_match.group(1)), Path(do_match.group(1))


                def main() -> int:
                    wrapper = Path(sys.argv[-1])
                    run_log_path, working_dir, input_do = parse_wrapper(wrapper)
                    working_dir.mkdir(parents=True, exist_ok=True)
                    commands = input_do.read_text(encoding="utf-8")
                    lines = []
                    rc = 0

                    for raw in commands.splitlines():
                        line = raw.strip()
                        if not line:
                            continue
                        if line.startswith("FAKE_SLEEP "):
                            time.sleep(float(line.split(" ", 1)[1]))
                            continue
                        if line.startswith("FAKE_WRITE "):
                            target = working_dir / line.split(" ", 1)[1]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text("ok", encoding="utf-8")
                            lines.append(f"wrote {target}")
                            continue
                        if line.startswith("FAKE_ERROR "):
                            payload = line.split(" ", 1)[1]
                            code_text, message = payload.split("|", 1)
                            rc = int(code_text)
                            lines.append(message)
                            lines.append(f"r({rc});")
                            break
                        lines.append(f". {line}")

                    lines.append(f"__AGENT_RC__={rc}")
                    run_log_path.parent.mkdir(parents=True, exist_ok=True)
                    run_log_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")

                    process_log = Path.cwd() / f"{wrapper.stem}.log"
                    process_lines = ["outer process header", *lines]
                    process_log.write_text("\\n".join(process_lines) + "\\n", encoding="utf-8")
                    return rc


                if __name__ == "__main__":
                    raise SystemExit(main())
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        fake_cmd = root / "fake_stata.cmd"
        fake_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake_stata.py" %*\r\n',
            encoding="utf-8",
        )
        return fake_cmd

    def _send_mcp(self, process: subprocess.Popen[str], payload: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _read_mcp(self, process: subprocess.Popen[str]) -> dict[str, object]:
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise AssertionError(f"Expected MCP response, got EOF. stderr={stderr!r}")
        return json.loads(line)

    def _workspace_case_dir(self) -> Path:
        base = Path.cwd() / ".tmp_test_runs"
        base.mkdir(parents=True, exist_ok=True)
        root = base / f"case_{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        self._case_dirs.append(root)
        return root


if __name__ == "__main__":
    unittest.main()
