import json
from pathlib import Path

from reforge.backend.app.agents.testing import TestRunner
from reforge.backend.app.tools.interfaces import ExecResult


FIXTURE_WORKSPACE = Path("reforge/backend/tests/fixtures/workspace")


class FakeSandbox:
    def __init__(
        self,
        workspace: Path,
        results: list[ExecResult],
        pytest_payload: dict[str, object] | None = None,
    ) -> None:
        self.workspace = workspace
        self.results = results
        self.pytest_payload = pytest_payload
        self.calls: list[tuple[list[str], str]] = []

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        if phase == "run" and not any(call_phase == "run" for _, call_phase in self.calls):
            if self.pytest_payload is not None:
                artifact = self.workspace / ".reforge-artifacts" / "pytest-report.json"
                artifact.parent.mkdir(exist_ok=True)
                artifact.write_text(json.dumps(self.pytest_payload), encoding="utf-8")
        self.calls.append((cmd, phase))
        return self.results.pop(0)


def result(*, code: int = 0, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(code, stdout, stderr, 0.01, False)


def test_runner_parses_pytest_and_runs_network_isolated_checks() -> None:
    sandbox = FakeSandbox(
        FIXTURE_WORKSPACE,
        [result(), result(code=1), result(code=1, stdout="[{\"code\": \"E501\"}]")],
        {
            "tests": [
                {"nodeid": "tests/test_a.py::test_ok", "outcome": "passed"},
                {
                    "nodeid": "tests/test_a.py::test_bad",
                    "outcome": "failed",
                    "call": {"longrepr": "AssertionError: expected 1 == 2"},
                },
                {
                    "nodeid": "tests/test_a.py::test_setup",
                    "outcome": "failed",
                    "setup": {"longrepr": "RuntimeError: setup broken"},
                },
                {"nodeid": "tests/test_a.py::test_skip", "outcome": "skipped"},
            ]
        },
    )

    report = TestRunner(sandbox, FIXTURE_WORKSPACE).run("baseline")

    assert report.install_ok is True
    assert report.passed == ["tests/test_a.py::test_ok"]
    assert [failure.test_id for failure in report.failed] == ["tests/test_a.py::test_bad"]
    assert [failure.test_id for failure in report.errored] == ["tests/test_a.py::test_setup"]
    assert report.skipped == ["tests/test_a.py::test_skip"]
    assert report.lint_issues == 1
    assert [phase for _, phase in sandbox.calls] == ["install", "run", "run"]
    assert sandbox.calls[2][0][-1].endswith("--ignore EXE002")


def test_runner_stops_when_install_fails() -> None:
    sandbox = FakeSandbox(FIXTURE_WORKSPACE, [result(code=1, stderr="pip failed")])

    report = TestRunner(sandbox, FIXTURE_WORKSPACE).run("baseline")

    assert report.install_ok is False
    assert "pip failed" in report.install_log_tail
    assert sandbox.calls[0][1] == "install"
    assert len(sandbox.calls) == 1


def test_default_install_uses_persistent_venv() -> None:
    command = TestRunner.default_install_command()

    assert "--system-site-packages .venv" in command
    assert "if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/pip ]" in command
    assert "requirements.txt" in command
    assert "pip install -e ." in command
    assert command.startswith("true &&")


def test_dependency_change_requests_clean_virtual_environment() -> None:
    command = TestRunner.default_install_command(recreate_venv=True)

    assert command.startswith("rm -rf .venv &&")
