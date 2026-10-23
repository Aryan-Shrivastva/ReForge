"""Deterministic test execution for Python and npm projects.

The runner makes no LLM decisions. The optional baseline test generator is a
later M6 concern; M1 establishes the runner it will use.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from ..core.state import Failure, TestReport
from ..tools.interfaces import ExecResult, Sandbox

_ARTIFACT_PATH = ".reforge-artifacts/pytest-report.json"


class TestRunner:
    """Install and evaluate a target repo in separated Docker network phases."""

    __test__ = False

    def __init__(
        self, sandbox: Sandbox, workspace: Path, *, timeout_s: int = 300
    ) -> None:
        self.sandbox = sandbox
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        if timeout_s < 1:
            raise ValueError("timeout_s must be positive")
        self.timeout_s = timeout_s

    def run(
        self,
        phase: Literal["baseline", "post_transform", "post_repair"],
        *,
        run_mypy: bool = False,
        install_command: str | None = None,
        recreate_venv: bool = False,
    ) -> TestReport:
        """Return a typed report from the repository's detected native toolchain."""
        if self._is_node_project():
            return self._run_node_project(phase, recreate_dependencies=recreate_venv)
        started = monotonic()
        artifact = self.workspace / _ARTIFACT_PATH
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if artifact.exists():
            artifact.unlink()

        install = self.sandbox.run(
            [
                "/bin/sh",
                "-lc",
                install_command or self.default_install_command(recreate_venv),
            ],
            phase="install",
            timeout=self.timeout_s,
        )
        if install.exit_code != 0 or install.timed_out:
            return TestReport(
                phase=phase,
                install_ok=False,
                install_log_tail=self._tail(install),
                duration_s=monotonic() - started,
                generated_tests=False,
            )

        pytest_result = self.sandbox.run(
            ["/bin/sh", "-lc", self.pytest_command()],
            phase="run",
            timeout=self.timeout_s,
        )
        payload = self._load_pytest_payload(artifact)
        report = self.parse_pytest_report(
            payload,
            phase=phase,
            install_log_tail=self._tail(install),
            duration_s=monotonic() - started,
        )
        if payload is None and pytest_result.exit_code != 0:
            report.errored.append(
                Failure(
                    test_id="<pytest>",
                    error_type="RunnerError",
                    message="pytest did not produce a JSON report",
                    traceback_excerpt=self._tail(pytest_result),
                    file=None,
                )
            )

        lint = self.sandbox.run(
            ["/bin/sh", "-lc", "ruff check . --output-format=json --ignore EXE002"],
            phase="run",
            timeout=self.timeout_s,
        )
        report.lint_issues = self._count_json_items(lint.stdout)
        if run_mypy:
            typecheck = self.sandbox.run(
                ["/bin/sh", "-lc", "mypy . --output=json"],
                phase="run",
                timeout=self.timeout_s,
            )
            report.typecheck_errors = self._count_json_items(typecheck.stdout)
        report.duration_s = monotonic() - started
        return report

    def _run_node_project(
        self,
        phase: Literal["baseline", "post_transform", "post_repair"],
        *,
        recreate_dependencies: bool,
    ) -> TestReport:
        """Run npm's test script, or a build check when no test script exists.

        A successful build is deliberately labelled as a build check, not a
        test suite.  This avoids manufacturing Python file-presence checks for
        a JavaScript/TypeScript repository.
        """
        started = monotonic()
        install = self.sandbox.run(
            ["/bin/sh", "-lc", self.node_install_command(recreate_dependencies)],
            phase="install",
            timeout=self.timeout_s,
        )
        if install.exit_code != 0 or install.timed_out:
            return TestReport(
                phase=phase,
                install_ok=False,
                install_log_tail=self._tail(install),
                duration_s=monotonic() - started,
            )
        has_test_script = self._package_has_script("test")
        command = "npm run test" if has_test_script else "npm run build"
        check = self.sandbox.run(["/bin/sh", "-lc", command], phase="run", timeout=self.timeout_s)
        passed = ["<npm-test>"] if has_test_script and check.exit_code == 0 else ["<npm-build>"] if check.exit_code == 0 else []
        failed = []
        if check.exit_code != 0 or check.timed_out:
            failed.append(
                Failure(
                    test_id="<npm-test>" if has_test_script else "<npm-build>",
                    error_type="RunnerError",
                    message="npm test command failed" if has_test_script else "npm build check failed",
                    traceback_excerpt=self._tail(check),
                    file="package.json",
                )
            )
        return TestReport(
            phase=phase,
            install_ok=True,
            install_log_tail=self._tail(install),
            passed=passed,
            failed=failed,
            duration_s=monotonic() - started,
            # The native test/build command is the baseline gate. Running an
            # additional lint script here repeats a full project traversal,
            # often after a build has already failed, and can double live-demo
            # time without providing another behavior-preservation signal.
            lint_issues=None,
        )

    @staticmethod
    def default_install_command(recreate_venv: bool = False) -> str:
        """Create a persistent venv, or cleanly rebuild it after dependency changes."""
        commands = [
            "rm -rf .venv" if recreate_venv else "true",
            "if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/pip ]; then rm -rf .venv && python -m venv --system-site-packages .venv; fi",
            "if [ -f requirements.txt ]; then .venv/bin/pip install -r requirements.txt; fi",
            "if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ]; then .venv/bin/pip install -e .; fi",
        ]
        return " && ".join(commands)

    def node_install_command(self, recreate_dependencies: bool = False) -> str:
        installer = "npm ci --no-audit --no-fund" if (self.workspace / "package-lock.json").is_file() else "npm install --no-audit --no-fund"
        if recreate_dependencies:
            return f"rm -rf node_modules && {installer}"
        return f"if [ ! -d node_modules ]; then {installer}; fi"

    def _is_node_project(self) -> bool:
        return (self.workspace / "package.json").is_file()

    def _package_has_script(self, name: str) -> bool:
        try:
            package = json.loads((self.workspace / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
        return isinstance(scripts, dict) and isinstance(scripts.get(name), str)

    @staticmethod
    def pytest_command() -> str:
        report_arg = shlex.quote(_ARTIFACT_PATH)
        return (
            "mkdir -p .reforge-artifacts && "
            ".venv/bin/python -m pytest --json-report "
            f"--json-report-file={report_arg}"
        )

    @classmethod
    def parse_pytest_report(
        cls,
        payload: dict[str, Any] | None,
        *,
        phase: Literal["baseline", "post_transform", "post_repair"],
        install_log_tail: str,
        duration_s: float,
    ) -> TestReport:
        """Convert pytest-json-report data to ReForge's stable contract."""
        passed: list[str] = []
        skipped: list[str] = []
        failed: list[Failure] = []
        errored: list[Failure] = []
        for test in (payload or {}).get("tests", []):
            nodeid = str(test.get("nodeid", "<unknown>"))
            outcome = test.get("outcome")
            if outcome == "passed":
                passed.append(nodeid)
            elif outcome == "skipped":
                skipped.append(nodeid)
            elif outcome == "failed":
                failure = cls._failure_from_test(test)
                if cls._is_test_error(test):
                    errored.append(failure)
                else:
                    failed.append(failure)
        return TestReport(
            phase=phase,
            install_ok=True,
            install_log_tail=install_log_tail,
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            duration_s=duration_s,
            generated_tests=False,
        )

    @staticmethod
    def _failure_from_test(test: dict[str, Any]) -> Failure:
        nodeid = str(test.get("nodeid", "<unknown>"))
        longrepr = ""
        for phase in ("setup", "call", "teardown"):
            details = test.get(phase)
            if isinstance(details, dict) and details.get("longrepr"):
                longrepr = str(details["longrepr"])
                break
        first_line = longrepr.strip().splitlines()[0] if longrepr.strip() else "test failed"
        return Failure(
            test_id=nodeid,
            error_type=first_line.split(":", 1)[0],
            message=first_line,
            traceback_excerpt=longrepr[-4_000:],
            file=nodeid.split("::", 1)[0] if "::" in nodeid else None,
        )

    @staticmethod
    def _is_test_error(test: dict[str, Any]) -> bool:
        return any(
            isinstance(test.get(phase), dict) and test[phase].get("longrepr")
            for phase in ("setup", "teardown")
        )

    @staticmethod
    def _load_pytest_payload(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _count_json_items(output: str) -> int | None:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return None
        return len(value) if isinstance(value, list) else None

    @staticmethod
    def _tail(result: ExecResult, *, limit: int = 4_000) -> str:
        return f"{result.stdout}\n{result.stderr}".strip()[-limit:]
