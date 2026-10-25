"""Repositories without a test suite: node:test characterization tests and the syntax-check baseline."""

from __future__ import annotations

import json
from pathlib import Path

from reforge.backend.app.agents.testing import TestRunner
from reforge.backend.app.core.events import RunStateStore
from reforge.backend.app.core.state import TestReport as StateTestReport
from reforge.backend.app.core.workflow import WorkflowRunner
from reforge.backend.app.tools.ast_tools import RuleCatalog
from reforge.backend.app.tools.interfaces import ExecResult
from reforge.backend.tests.conftest import make_state
from reforge.backend.tests.test_workflow import (
    FakeAnalysisAgent,
    FakePlanningAgent,
    FakeTransformationAgent,
    FakeWorkflowGit,
    isolated_workspace,
)

# Captured from `node --test --test-reporter=junit` on Node 22.
NODE_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
	<testsuite name="add" time="0.01" disabled="0" errors="0" tests="2" failures="1" skipped="0">
		<testcase name="adds numbers" time="0.002" classname="test"/>
		<testcase name="fails on purpose" time="0.005" classname="test" failure="Expected values to be strictly equal:3 !== 4">
			<failure type="testCodeFailure" message="Expected values to be strictly equal:3 !== 4">AssertionError: 3 !== 4</failure>
		</testcase>
		<testcase name="later" time="0" classname="test"><skipped type="skipped" message="TODO"/></testcase>
	</testsuite>
	<testcase name="top level passes" time="0.0005" classname="test"/>
</testsuites>
"""


def test_node_junit_output_becomes_path_qualified_test_ids() -> None:
    report = TestRunner.parse_node_junit(NODE_JUNIT, "tests/reforge_generated/lib.test.js")

    assert report.passed == [
        "tests/reforge_generated/lib.test.js::add adds numbers",
        "tests/reforge_generated/lib.test.js::top level passes",
    ]
    assert [failure.test_id for failure in report.failed] == ["tests/reforge_generated/lib.test.js::add fails on purpose"]
    assert report.failed[0].file == "tests/reforge_generated/lib.test.js"
    assert report.skipped == ["tests/reforge_generated/lib.test.js::add later"]


class ScriptedSandbox:
    """Answers node --test runs by writing JUnit files and smoke checks by printing results."""

    def __init__(self, workspace: Path, smoke_failures: set[str] | None = None) -> None:
        self.workspace = workspace
        self.smoke_failures = smoke_failures or set()
        self.commands: list[str] = []

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        command = cmd[-1]
        self.commands.append(command)
        if "node --test" in command:
            for index in range(command.count("node --test")):
                target = self.workspace / ".reforge-artifacts" / "node-tests" / f"{index}.xml"
                target.write_text(NODE_JUNIT, encoding="utf-8")
        if "REFORGE_SMOKE" in command:
            files = (self.workspace / ".reforge-artifacts" / "smoke-files.txt").read_text(encoding="utf-8").split()
            lines = [f"REFORGE_SMOKE {'fail' if file in self.smoke_failures else 'ok'} {file}" for file in files]
            return ExecResult(0, "\n".join(lines), "", 0.01, False)
        return ExecResult(0, "", "", 0.01, False)


def frameworkless_project(root: Path) -> Path:
    files = {
        "package.json": json.dumps({"name": "app", "scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        "package-lock.json": "{}",
        "server.js": "require('express')().listen(3000);\n",
        "lib/format.js": "module.exports = (value) => String(value);\n",
        "public/app.min.js": "var a=1;\n",
        "tools/build.py": "print('x')\n",
        "tests/reforge_generated/format.test.js": "require('node:test');\n",
    }
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_generated_tests_run_on_node_test_runner_when_the_project_has_no_framework(tmp_path: Path) -> None:
    root = frameworkless_project(tmp_path)
    sandbox = ScriptedSandbox(root)

    report = TestRunner(sandbox, root).run("baseline")

    node_runs = [command for command in sandbox.commands if "node --test" in command]
    assert len(node_runs) == 1
    assert "--test-reporter=junit --test-reporter-destination=.reforge-artifacts/node-tests/0.xml" in node_runs[0]
    assert node_runs[0].endswith("tests/reforge_generated/format.test.js; true")
    assert "tests/reforge_generated/format.test.js::add adds numbers" in report.passed
    assert not any("npm run test" in command for command in sandbox.commands)  # placeholder script is not run


def test_smoke_checks_cover_source_files_but_not_minified_or_generated_ones(tmp_path: Path) -> None:
    root = frameworkless_project(tmp_path)
    sandbox = ScriptedSandbox(root, smoke_failures={"server.js"})

    report = TestRunner(sandbox, root).run("baseline", smoke_checks=True)

    smoke_passed = sorted(test for test in report.passed if test.startswith("smoke:"))
    assert smoke_passed == ["smoke:syntax:lib/format.js", "smoke:syntax:tools/build.py"]
    assert [failure.test_id for failure in report.failed if failure.test_id.startswith("smoke:")] == ["smoke:syntax:server.js"]
    assert all("::" not in test for test in smoke_passed)  # never mistaken for protected test files


class SmokeOnlyRunner:
    """No real tests; syntax checks pass when requested."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def run(self, phase: str, **kwargs: object) -> StateTestReport:
        smoke = bool(kwargs.get("smoke_checks"))
        self.calls.append((phase, smoke))
        return StateTestReport(
            phase=phase,  # type: ignore[arg-type]
            install_ok=True,
            passed=["smoke:syntax:lib/format.js", "smoke:syntax:server.js"] if smoke else [],
            duration_s=0.01,
        )


def test_repository_without_tests_proceeds_on_a_syntax_check_baseline(tmp_path: Path) -> None:
    testing = SmokeOnlyRunner()
    runner = WorkflowRunner(
        analysis_agent=FakeAnalysisAgent(),  # type: ignore[arg-type]
        planning_agent=FakePlanningAgent(),  # type: ignore[arg-type]
        transformation_agent=FakeTransformationAgent(),  # type: ignore[arg-type]
        testing_runner_factory=lambda _: testing,
        catalog=RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml")),
        state_store=RunStateStore(Path(".workflow-test-runs") / "smoke"),
        min_baseline_passing_tests=1,
        git_factory=FakeWorkflowGit,  # type: ignore[arg-type]
    )
    state = make_state(run_id="smoke-baseline", objective="Modernize dependency", workspace_path=str(isolated_workspace(tmp_path)))

    result = runner.run(state)

    assert result.smoke_baseline is True
    assert result.baseline_pass_set == ["smoke:syntax:lib/format.js", "smoke:syntax:server.js"]
    assert result.final_status is not None and result.final_status.value == "SUCCESS"
    # Baseline without checks, the syntax-check fallback, then verification with checks.
    assert testing.calls == [("baseline", False), ("baseline", True), ("post_transform", True)]
    assert any("reduced baseline" in event.summary for event in result.trace)
