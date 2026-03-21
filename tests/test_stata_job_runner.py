from __future__ import annotations

import json
from pathlib import Path
import sys
import textwrap
import unittest
import uuid

from infra import JobSpec, StataConfig, StataJobRunner


class StataJobRunnerTests(unittest.TestCase):
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

        resolved = runner._resolve_subprocess_executable()

        self.assertEqual(resolved, headless.resolve())

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
        command = runner._build_subprocess_command(fake_exe, job.wrapper_do_path)

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
                        lines.append(line)

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

    def _workspace_case_dir(self) -> Path:
        base = Path.cwd() / ".tmp_test_runs"
        base.mkdir(parents=True, exist_ok=True)
        root = base / f"case_{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)
        return root


if __name__ == "__main__":
    unittest.main()
