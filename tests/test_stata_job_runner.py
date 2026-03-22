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

from infra import JobSpec, StataConfig, StataJobRunner
from infra.executable_resolver import build_stata_command, resolve_stata_executable


class StataJobRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._case_dirs: list[Path] = []

    def tearDown(self) -> None:
        if os.getenv("KEEP_TEST_ARTIFACTS", "0") == "1":
            return
        for case_dir in self._case_dirs:
            shutil.rmtree(case_dir, ignore_errors=True)

    def test_missing_script_returns_input_error_and_manifest(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        result = runner.run_do("missing.do", JobSpec())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.phase, "input")
        self.assertEqual(result.error_kind, "input_error")
        self.assertIsNone(result.run_log_path)
        self.assertIsNone(result.process_log_path)
        self.assertTrue((Path(result.job_dir) / "result.json").exists())

    def test_bootstrap_error_when_executable_cannot_be_resolved(self) -> None:
        root = self._workspace_case_dir()
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(root / "missing.exe"),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        result = runner.run_inline("display 1", JobSpec())

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.phase, "bootstrap")
        self.assertEqual(result.error_kind, "bootstrap_error")
        self.assertIsNone(result.process_log_path)

    def test_executable_resolution_prefers_headless_candidate(self) -> None:
        root = self._workspace_case_dir()
        install_dir = root / "stata17"
        install_dir.mkdir(parents=True, exist_ok=True)
        gui = install_dir / "StataMP-64.exe"
        headless = install_dir / "StataMP-console.exe"
        gui.write_text("", encoding="utf-8")
        headless.write_text("", encoding="utf-8")
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(install_dir),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        resolved = resolve_stata_executable(str(install_dir), "mp")

        self.assertEqual(resolved, headless.resolve())

    def test_executable_resolution_returns_none_without_explicit_stata_path(self) -> None:
        resolved = resolve_stata_executable(None, "mp")

        self.assertIsNone(resolved)

    def test_wrapper_requests_full_stata_exit_and_windows_batch_flags(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        job = runner._create_job_context(JobSpec())
        runner._write_wrapper_do(job)
        wrapper_text = job.wrapper_do_path.read_text(encoding="utf-8")
        command = build_stata_command(fake_exe, job.wrapper_do_path)

        self.assertIn("exit `agent_rc', STATA clear", wrapper_text)
        if sys.platform.startswith("win"):
            self.assertEqual(command[1:4], ["/q", "/i", "/e"])

    def test_run_do_resolves_relative_paths_and_deletes_redundant_process_log(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        working_dir = root / "workspace"
        working_dir.mkdir(parents=True, exist_ok=True)
        (working_dir / "analysis.do").write_text("FAKE_WRITE output/result.txt\n", encoding="utf-8")
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=working_dir,
                job_root=root / "jobs",
            )
        )

        result = runner.run_do(
            "analysis.do",
            JobSpec(
                artifact_globs=("output/**/*.txt",),
                timeout_sec=5,
            ),
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.phase, "completed")
        self.assertEqual(result.artifacts, [str((working_dir / "output" / "result.txt").resolve())])
        self.assertTrue((Path(result.job_dir) / "input.do").exists())
        self.assertTrue((Path(result.job_dir) / "wrapper.do").exists())
        self.assertTrue((Path(result.job_dir) / "result.json").exists())
        self.assertEqual(Path(result.run_log_path).parent, Path(result.job_dir))
        self.assertIsNone(result.process_log_path)
        self.assertFalse((Path(result.job_dir) / "wrapper.log").exists())
        self.assertFalse((Path(result.job_dir) / "process.log").exists())
        self.assertFalse((root / "wrapper.log").exists())
        self.assertFalse((working_dir / "wrapper.log").exists())

    def test_run_inline_returns_parse_error_summary(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        result = runner.run_inline("FAKE_ERROR 199|command foo is unrecognized", JobSpec(timeout_sec=5))

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_kind, "stata_parse_or_command_error")
        self.assertIn("command foo is unrecognized", result.summary)
        self.assertNotEqual(result.summary.strip(), "Stata execution failed with exit_code=199: r(199);")

    def test_failed_job_returns_mechanical_diagnostics(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        result = runner.run_inline(
            "regress price weight mpg\nFAKE_ERROR 111|variable mpg not found",
            JobSpec(timeout_sec=5),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_signature, "variable mpg not found")
        self.assertEqual(result.failed_command, "regress price weight mpg")
        self.assertIn(". regress price weight mpg", result.diagnostic_excerpt)
        self.assertIn("variable mpg not found", result.diagnostic_excerpt)
        self.assertNotIn("__AGENT_RC__", result.diagnostic_excerpt)

    def test_success_job_returns_last_command_diagnostic_excerpt_without_semantic_parsing(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        result = runner.run_inline(
            "display 1\nsummarize price",
            JobSpec(timeout_sec=5),
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIsNone(result.error_signature)
        self.assertEqual(result.failed_command, "summarize price")
        self.assertIn(". summarize price", result.diagnostic_excerpt)
        self.assertNotIn("__AGENT_RC__", result.diagnostic_excerpt)

    def test_failed_job_still_collects_artifacts(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        working_dir = root / "wd"
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=working_dir,
                job_root=root / "jobs",
            )
        )

        result = runner.run_inline(
            "FAKE_WRITE reports/partial.txt\nFAKE_ERROR 199|command foo is unrecognized",
            JobSpec(timeout_sec=5, artifact_globs=("reports/**/*.txt",)),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_kind, "stata_parse_or_command_error")
        self.assertEqual(result.artifacts, [str((working_dir / "reports" / "partial.txt").resolve())])

    def test_timeout_terminates_subprocess_and_next_job_is_clean(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        timed_out = runner.run_inline("FAKE_SLEEP 2", JobSpec(timeout_sec=1))
        succeeded = runner.run_inline(
            "FAKE_WRITE reports/ok.txt",
            JobSpec(timeout_sec=5, artifact_globs=("reports/**/*.txt",)),
        )

        self.assertEqual(timed_out.status, "failed")
        self.assertEqual(timed_out.error_kind, "timeout")
        self.assertEqual(succeeded.status, "succeeded")
        self.assertNotEqual(timed_out.job_dir, succeeded.job_dir)

    def test_jobs_keep_isolated_manifests(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        runner = StataJobRunner(
            StataConfig(
                stata_path=str(fake_exe),
                working_dir=root / "wd",
                job_root=root / "jobs",
            )
        )

        first = runner.run_inline("FAKE_WRITE a/one.txt", JobSpec(timeout_sec=5, artifact_globs=("a/**/*.txt",)))
        second = runner.run_inline("FAKE_WRITE b/two.txt", JobSpec(timeout_sec=5, artifact_globs=("b/**/*.txt",)))

        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertNotEqual(first.job_dir, second.job_dir)
        self.assertTrue((Path(first.job_dir) / "result.json").exists())
        self.assertTrue((Path(second.job_dir) / "result.json").exists())
        first_manifest = json.loads((Path(first.job_dir) / "result.json").read_text(encoding="utf-8"))
        second_manifest = json.loads((Path(second.job_dir) / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(first_manifest["status"], "succeeded")
        self.assertEqual(second_manifest["status"], "succeeded")
        self.assertIn("run_log_path", first_manifest)
        self.assertIn("process_log_path", second_manifest)
        self.assertIsNone(first_manifest["process_log_path"])
        self.assertIsNone(second_manifest["process_log_path"])

    def test_cli_argument_errors_return_stable_json_protocol(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "main.py",
                "run-inline",
                "display 1",
                "--stata-path",
                "D:/missing/stata.exe",
                "--env",
                "INVALID",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stderr.strip(), "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["phase"], "input")
        self.assertEqual(payload["error_kind"], "input_error")
        self.assertEqual(payload["exit_code"], 2)
        self.assertIsNone(payload["job_dir"])
        self.assertIsNone(payload["run_log_path"])
        self.assertIsNone(payload["process_log_path"])
        self.assertEqual(payload["diagnostic_excerpt"], "")
        self.assertIsNone(payload["error_signature"])
        self.assertIsNone(payload["failed_command"])

    def test_skill_wrapper_runs_job_via_repo_cli(self) -> None:
        root = self._workspace_case_dir()
        fake_exe = self._create_fake_stata_executable(root)
        fake_uv = self._create_fake_uv_executable(root)
        working_dir = root / "skill-work"
        working_dir.mkdir(parents=True, exist_ok=True)
        script_path = working_dir / "analysis.do"
        script_path.write_text("FAKE_WRITE output/result.txt\n", encoding="utf-8")
        skill_script = PROJECT_ROOT / "skills" / "stata-job-runner" / "scripts" / "run_stata_job.py"

        completed = subprocess.run(
            [
                sys.executable,
                str(skill_script),
                "run-do",
                str(script_path),
                "--repo-root",
                str(PROJECT_ROOT),
                "--uv-path",
                str(fake_uv),
                "--stata-path",
                str(fake_exe),
                "--working-dir",
                str(working_dir),
                "--job-root",
                str(root / "jobs"),
                "--artifact-glob",
                "output/**/*.txt",
                "--compact",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr.strip(), "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["process_log_path"], None)
        self.assertEqual(
            payload["artifacts"],
            [str((working_dir / "output" / "result.txt").resolve())],
        )

    def test_skill_wrapper_falls_back_to_repo_venv_python_when_uv_is_missing(self) -> None:
        root = self._workspace_case_dir()
        repo_root = self._create_minimal_repo(root, summary="ok-from-venv")
        self._create_fake_repo_python(repo_root)
        skill_script = PROJECT_ROOT / "skills" / "stata-job-runner" / "scripts" / "run_stata_job.py"
        empty_home = root / "home"
        empty_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        env["HOME"] = str(empty_home)
        env["USERPROFILE"] = str(empty_home)
        env["UV_EXE"] = ""

        completed = subprocess.run(
            [
                sys.executable,
                str(skill_script),
                "run-inline",
                "display 1",
                "--repo-root",
                str(repo_root),
                "--stata-path",
                "D:/missing/stata.exe",
                "--compact",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["summary"], "ok-from-venv")

    def test_skill_wrapper_returns_bootstrap_json_when_no_runner_is_available(self) -> None:
        root = self._workspace_case_dir()
        repo_root = self._create_minimal_repo(root)
        skill_script = PROJECT_ROOT / "skills" / "stata-job-runner" / "scripts" / "run_stata_job.py"
        empty_home = root / "home"
        empty_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["PATH"] = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
        env["HOME"] = str(empty_home)
        env["USERPROFILE"] = str(empty_home)
        env["UV_EXE"] = ""

        completed = subprocess.run(
            [
                sys.executable,
                str(skill_script),
                "run-inline",
                "display 1",
                "--repo-root",
                str(repo_root),
                "--stata-path",
                "D:/missing/stata.exe",
                "--compact",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["phase"], "bootstrap")
        self.assertEqual(payload["error_kind"], "bootstrap_error")
        self.assertIn("Cannot locate a working uv executable or repo-local .venv Python", payload["summary"])

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

    def _create_minimal_repo(self, root: Path, summary: str = "ok") -> Path:
        repo_root = root / "mini-repo"
        (repo_root / "infra").mkdir(parents=True, exist_ok=True)
        (repo_root / "infra" / "__init__.py").write_text("# test repo marker\n", encoding="utf-8")
        payload = {
            "status": "succeeded",
            "phase": "completed",
            "exit_code": 0,
            "error_kind": None,
            "summary": summary,
            "job_dir": None,
            "run_log_path": None,
            "process_log_path": None,
            "log_tail": "",
            "artifacts": [],
            "elapsed_ms": 0,
            "working_dir": str(repo_root),
            "diagnostic_excerpt": "",
            "error_signature": None,
            "failed_command": None,
        }
        main_text = textwrap.dedent(
            f"""
            from __future__ import annotations

            import json


            if __name__ == "__main__":
                print(json.dumps({payload!r}, ensure_ascii=False))
            """
        ).strip() + "\n"
        (repo_root / "main.py").write_text(main_text, encoding="utf-8")
        return repo_root

    def _create_fake_repo_python(self, repo_root: Path) -> Path:
        scripts_dir = repo_root / ".venv" / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        fake_cmd = scripts_dir / "python.cmd"
        fake_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" %*\r\n',
            encoding="utf-8",
        )
        return fake_cmd

    def _create_fake_uv_executable(self, root: Path) -> Path:
        fake_py = root / "fake_uv.py"
        fake_py.write_text(
            textwrap.dedent(
                """
                from __future__ import annotations

                import subprocess
                import sys


                def main() -> int:
                    args = sys.argv[1:]
                    if args == ["--version"]:
                        print("uv 0.fake")
                        return 0
                    if args and args[0] == "run":
                        args = args[1:]
                    completed = subprocess.run(args, check=False)
                    return completed.returncode


                if __name__ == "__main__":
                    raise SystemExit(main())
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        fake_cmd = root / "fake_uv.cmd"
        fake_cmd.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0fake_uv.py" %*\r\n',
            encoding="utf-8",
        )
        return fake_cmd

    def _workspace_case_dir(self) -> Path:
        base = Path.cwd() / ".tmp_test_runs"
        base.mkdir(parents=True, exist_ok=True)
        root = base / f"case_{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        self._case_dirs.append(root)
        return root


if __name__ == "__main__":
    unittest.main()
