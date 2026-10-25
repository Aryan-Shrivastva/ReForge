"""Deterministic test execution for Python and JavaScript/TypeScript projects.

The runner makes no LLM decisions. The optional baseline test generator is a
later M6 concern; M1 establishes the runner it will use.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from ..core.state import Failure, TestReport
from ..tools.interfaces import ExecResult, Sandbox
from ..tools.languages import (
    JS_LIKE_EXTENSIONS,
    detect_js_package_manager,
    detect_js_test_framework,
    is_js_test_file,
    is_python_test_file,
    iter_source_files,
    package_dependency_names,
    package_scripts,
    read_package_json,
)

_ARTIFACT_PATH = ".reforge-artifacts/pytest-report.json"
_JS_ARTIFACT_PATH = ".reforge-artifacts/js-test-report.json"
# npm's generated placeholder: "echo \"Error: no test specified\" && exit 1".
_NPM_PLACEHOLDER_TEST = re.compile(r"no test specified")
_TS_ERROR = re.compile(r"error TS\d+")
_MOCHA_HOOK = re.compile(r'"(?:before|after) (?:all|each)" hook')
_PYTEST_CONFIG_FILES = ("pytest.ini", "conftest.py")


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
        """Return a typed report from the repository's detected native toolchain(s).

        A polyglot repository (for example a Python API with a JavaScript
        frontend) runs both suites; the reports are merged so the baseline
        protects both.
        """
        if not self._is_node_project():
            return self._run_python_project(
                phase, run_mypy=run_mypy, install_command=install_command, recreate_venv=recreate_venv
            )
        # With a package.json present, Python metadata alone (e.g. a tooling-only
        # pyproject.toml) is not a Python suite; only real pytest tests are merged.
        if self._has_python_tests():
            python_report = self._run_python_project(
                phase, run_mypy=run_mypy, install_command=install_command, recreate_venv=recreate_venv
            )
            node_report = self._run_node_project(phase, recreate_dependencies=recreate_venv)
            return self.merge_reports(python_report, node_report)
        return self._run_node_project(phase, recreate_dependencies=recreate_venv)

    @staticmethod
    def merge_reports(first: TestReport, second: TestReport) -> TestReport:
        """Combine per-toolchain reports; a failed install anywhere fails the whole install."""

        def total(left: int | None, right: int | None) -> int | None:
            return None if left is None and right is None else (left or 0) + (right or 0)

        return TestReport(
            phase=first.phase,
            install_ok=first.install_ok and second.install_ok,
            install_log_tail=f"{first.install_log_tail}\n{second.install_log_tail}".strip()[-4_000:],
            passed=[*first.passed, *second.passed],
            failed=[*first.failed, *second.failed],
            errored=[*first.errored, *second.errored],
            skipped=[*first.skipped, *second.skipped],
            duration_s=first.duration_s + second.duration_s,
            lint_issues=total(first.lint_issues, second.lint_issues),
            typecheck_errors=total(first.typecheck_errors, second.typecheck_errors),
            generated_tests=first.generated_tests or second.generated_tests,
        )

    def _run_python_project(
        self,
        phase: Literal["baseline", "post_transform", "post_repair"],
        *,
        run_mypy: bool,
        install_command: str | None,
        recreate_venv: bool,
    ) -> TestReport:
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
        """Run the JavaScript/TypeScript test suite with per-test results when possible.

        Jest, Vitest, and Mocha are run with their JSON reporters so the
        baseline freezes individual test ids (``path::full name``), exactly as
        pytest node ids are frozen for Python.  Other ``npm test`` scripts are
        a single coarse ``<npm-test>`` check, and a project without tests falls
        back to a build check.  A successful build is deliberately labelled as
        a build check, not a test suite.
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
        scripts = package_scripts(read_package_json(self.workspace))
        test_script = scripts.get("test", "")
        has_test_script = bool(test_script) and not _NPM_PLACEHOLDER_TEST.search(test_script)
        framework = detect_js_test_framework(self.workspace)

        report: TestReport | None = None
        if framework is not None and (has_test_script or self._has_js_test_files()):
            artifact = self.workspace / _JS_ARTIFACT_PATH
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if artifact.exists():
                artifact.unlink()
            check = self.sandbox.run(
                ["/bin/sh", "-lc", self.js_test_command(framework, test_script)],
                phase="run",
                timeout=self.timeout_s,
            )
            payload = self._load_pytest_payload(artifact)
            if payload is not None:
                report = self.parse_js_report(
                    payload,
                    framework=framework,
                    phase=phase,
                    install_log_tail=self._tail(install),
                    duration_s=monotonic() - started,
                    roots=("/work", self.workspace.as_posix()),
                )
                if check.timed_out:
                    report.errored.append(self._runner_failure("<js-tests>", "JavaScript test run timed out", check))
        if report is None:
            command = "npm run test" if has_test_script else "npm run build" if "build" in scripts else None
            if command is None:
                return TestReport(
                    phase=phase,
                    install_ok=True,
                    install_log_tail=self._tail(install),
                    duration_s=monotonic() - started,
                )
            check = self.sandbox.run(["/bin/sh", "-lc", command], phase="run", timeout=self.timeout_s)
            test_id = "<npm-test>" if has_test_script else "<npm-build>"
            succeeded = check.exit_code == 0 and not check.timed_out
            report = TestReport(
                phase=phase,
                install_ok=True,
                install_log_tail=self._tail(install),
                passed=[test_id] if succeeded else [],
                failed=[] if succeeded else [
                    self._runner_failure(
                        test_id,
                        "npm test command failed" if has_test_script else "npm build check failed",
                        check,
                    )
                ],
                duration_s=monotonic() - started,
            )
        # The native test command is the baseline gate.  ESLint is not run:
        # it repeats a full project traversal and is not a behaviour signal.
        # TypeScript type errors are, so `tsc --noEmit` is counted when the
        # project declares TypeScript.
        if self._uses_typescript():
            typecheck = self.sandbox.run(
                ["/bin/sh", "-lc", "npx --no-install tsc --noEmit --pretty false"],
                phase="run",
                timeout=self.timeout_s,
            )
            errors = len(_TS_ERROR.findall(f"{typecheck.stdout}\n{typecheck.stderr}"))
            # A non-zero exit without TS diagnostics means tsc itself could not
            # run; record "not measured" rather than a misleading zero.
            if not typecheck.timed_out and (typecheck.exit_code == 0 or errors):
                report.typecheck_errors = errors
        report.duration_s = monotonic() - started
        return report

    @staticmethod
    def js_test_command(framework: str, test_script: str) -> str:
        """Build a JSON-reporting test command that preserves the project's own test script.

        When ``npm test`` invokes the framework directly, reporter flags are
        appended through ``npm run test --`` so project arguments (config file,
        setup files, roots) are kept.  Otherwise the framework is run through
        ``npx --no-install`` so nothing is downloaded during the offline phase.
        """
        output = shlex.quote(_JS_ARTIFACT_PATH)
        flags = {
            "jest": f"--ci --json --outputFile={output} --testLocationInResults",
            "vitest": f"--run --reporter=json --outputFile={output}",
            "mocha": f"--reporter json --reporter-option output={output}",
        }[framework]
        direct = re.match(rf"^\s*(?:npx\s+)?{framework}(?:\s+run)?\b(?![^&|;]*[&|;])", test_script)
        if direct:
            command = f"npm run test -- {flags}"
        else:
            command = f"npx --no-install {framework}{' run' if framework == 'vitest' else ''} {flags}"
        return f"mkdir -p .reforge-artifacts && CI=true {command}"

    @classmethod
    def parse_js_report(
        cls,
        payload: dict[str, Any],
        *,
        framework: str,
        phase: Literal["baseline", "post_transform", "post_repair"],
        install_log_tail: str,
        duration_s: float,
        roots: tuple[str, ...] = ("/work",),
    ) -> TestReport:
        """Convert Jest/Vitest (shared format) or Mocha JSON into ReForge's contract.

        Reporters emit absolute file paths; ``roots`` (the sandbox mount and,
        when run elsewhere, the host workspace) are stripped so test ids are
        workspace-relative ``path::full name``, like pytest node ids.
        """

        def relative(file: object) -> str | None:
            return cls._js_relative(file, roots)

        def test_id(file: object, name: object) -> str:
            return f"{relative(file) or '<unknown>'}::{str(name or '<unnamed>').strip()}"

        passed: list[str] = []
        skipped: list[str] = []
        failed: list[Failure] = []
        errored: list[Failure] = []
        if framework == "mocha":
            for test in payload.get("passes", []) or []:
                passed.append(test_id(test.get("file"), test.get("fullTitle")))
            for test in payload.get("pending", []) or []:
                skipped.append(test_id(test.get("file"), test.get("fullTitle")))
            for test in payload.get("failures", []) or []:
                error = test.get("err") if isinstance(test.get("err"), dict) else {}
                title = str(test.get("fullTitle") or "")
                target = errored if _MOCHA_HOOK.search(title) else failed
                target.append(
                    cls._js_failure(
                        test_id(test.get("file"), title),
                        str(error.get("message") or "test failed"),
                        str(error.get("stack") or error.get("message") or ""),
                        relative(test.get("file")),
                    )
                )
        else:
            for suite in payload.get("testResults", []) or []:
                if not isinstance(suite, dict):
                    continue
                file = suite.get("name") or suite.get("testFilePath")
                assertions = suite.get("assertionResults") or []
                if not assertions and suite.get("status") == "failed":
                    message = str(suite.get("message") or suite.get("failureMessage") or "test file failed to run")
                    errored.append(cls._js_failure(test_id(file, "<suite>"), message, message, relative(file)))
                    continue
                for test in assertions:
                    if not isinstance(test, dict):
                        continue
                    name = test.get("fullName") or " ".join(
                        [*[str(part) for part in test.get("ancestorTitles") or []], str(test.get("title") or "")]
                    ).strip()
                    identifier = test_id(file, name)
                    status = test.get("status")
                    if status == "passed":
                        passed.append(identifier)
                    elif status in {"pending", "skipped", "todo", "disabled"}:
                        skipped.append(identifier)
                    elif status == "failed":
                        messages = [str(item) for item in test.get("failureMessages") or []]
                        detail = "\n".join(messages) or "test failed"
                        failed.append(cls._js_failure(identifier, detail.strip().splitlines()[0], detail, relative(file)))
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
    def _js_relative(file: object, roots: tuple[str, ...] = ("/work",)) -> str | None:
        if not isinstance(file, str) or not file:
            return None
        normalised = file.replace("\\", "/")
        for root in roots:
            prefix = root.replace("\\", "/").rstrip("/") + "/"
            if normalised.lower().startswith(prefix.lower()):
                return normalised[len(prefix):]
        return normalised.removeprefix("./")

    @staticmethod
    def _js_failure(test_id: str, message: str, detail: str, file: str | None) -> Failure:
        first_line = message.strip().splitlines()[0] if message.strip() else "test failed"
        return Failure(
            test_id=test_id,
            error_type=first_line.split(":", 1)[0][:120] or "Error",
            message=first_line[:500],
            traceback_excerpt=detail[-4_000:],
            file=file,
        )

    def _runner_failure(self, test_id: str, message: str, result: ExecResult) -> Failure:
        return Failure(
            test_id=test_id,
            error_type="RunnerError",
            message=message,
            traceback_excerpt=self._tail(result),
            file="package.json",
        )

    def _has_js_test_files(self) -> bool:
        return any(
            is_js_test_file(path.relative_to(self.workspace).as_posix())
            for path in iter_source_files(self.workspace, JS_LIKE_EXTENSIONS)
        )

    def _uses_typescript(self) -> bool:
        package = read_package_json(self.workspace)
        return (self.workspace / "tsconfig.json").is_file() and "typescript" in package_dependency_names(package)

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
        manager = detect_js_package_manager(self.workspace)
        if manager == "pnpm":
            # Corepack ships with Node and honours package.json "packageManager".
            installer = "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack pnpm install --frozen-lockfile"
        elif manager == "yarn":
            installer = "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack yarn install --immutable || COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack yarn install --frozen-lockfile"
        elif (self.workspace / "package-lock.json").is_file():
            installer = "npm ci --no-audit --no-fund"
        else:
            installer = "npm install --no-audit --no-fund"
        if recreate_dependencies:
            return f"rm -rf node_modules && {installer}"
        return f"if [ ! -d node_modules ]; then {installer}; fi"

    def _is_node_project(self) -> bool:
        return (self.workspace / "package.json").is_file()

    def _has_python_tests(self) -> bool:
        """A pytest configuration file or Python test files outside vendored directories."""
        if any((self.workspace / name).is_file() for name in _PYTEST_CONFIG_FILES):
            return True
        return any(
            is_python_test_file(path.relative_to(self.workspace).as_posix())
            for path in iter_source_files(self.workspace, (".py",))
        )

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
