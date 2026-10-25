"""JavaScript/TypeScript support: detection, scanning, analysis evidence, test runs, and generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reforge.backend.app.agents.analysis import collect_analysis_evidence
from reforge.backend.app.agents.test_generation import CharacterizationPathError, CharacterizationTestGenerator
from reforge.backend.app.agents.testing import TestRunner
from reforge.backend.app.core.state import GeneratedTestFile, TestGenerationProposal as GenerationProposal, TestReport as StateTestReport
from reforge.backend.app.tools.ast_tools import RuleCatalog, default_javascript_catalog, scan_all_deprecated_patterns
from reforge.backend.app.tools.interfaces import ExecResult
from reforge.backend.app.tools.languages import (
    detect_js_package_manager,
    detect_js_test_framework,
    detect_languages,
    is_test_infrastructure,
)

CATALOG = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))


def write(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def node_project(tmp_path: Path, *, framework: str = "jest", extra: dict[str, str] | None = None) -> Path:
    package = {
        "name": "legacy-node",
        "version": "1.0.0",
        "main": "src/index.js",
        "engines": {"node": ">=8"},
        "scripts": {"test": framework},
        "dependencies": {"request": "^2.88.0", "express": "^4.17.1"},
        "devDependencies": {framework: "^29.0.0"},
    }
    return write(
        tmp_path,
        {
            "package.json": json.dumps(package),
            "package-lock.json": "{}",
            "src/index.js": (
                "const request = require('request');\n"
                "const url = require('url');\n"
                "// new Buffer(1) in a comment is not reported\n"
                "function parse(input) { return url.parse(input).host; }\n"
                "function bytes(text) { return new Buffer(text); }\n"
                "module.exports = { parse, bytes };\n"
            ),
            "src/index.test.js": "const { parse } = require('./index');\nit('parses', () => { expect(parse('http://a.b/c')).toBe('a.b'); });\ntest.each([1, 2])('n %i', (n) => { expect(n).toBeTruthy(); });\n",
            "node_modules/dep/index.js": "new Buffer(1);\n",
            "dist/bundle.min.js": "new Buffer(1);\n",
            **(extra or {}),
        },
    )


def test_language_and_framework_detection(tmp_path: Path) -> None:
    root = node_project(tmp_path, framework="vitest", extra={"pnpm-lock.yaml": "", "tools/build.py": "print(1)\n"})

    assert detect_languages(["src/a.ts", "src/b.ts", "src/c.js", "x.py", "node_modules/z.js"]) == [
        "typescript", "javascript", "python"
    ]
    assert detect_js_test_framework(root) == "vitest"
    assert detect_js_package_manager(root) == "pnpm"


def test_javascript_rules_scan_source_but_skip_vendored_minified_and_comments(tmp_path: Path) -> None:
    root = node_project(tmp_path)

    findings = scan_all_deprecated_patterns(root, CATALOG)
    keys = sorted((item.rule_id, item.file, item.line) for item in findings)

    assert keys == [
        ("node-buffer-constructor", "src/index.js", 5),
        ("node-url-parse", "src/index.js", 4),
        ("npm-request-package", "src/index.js", 1),
    ]
    assert len(default_javascript_catalog().rules) >= 15


def test_typescript_only_rules_respect_extensions(tmp_path: Path) -> None:
    write(tmp_path, {"a.ts": "module Legacy {\n}\n", "b.js": "module Legacy {\n}\n"})

    rule_files = {(item.rule_id, item.file) for item in scan_all_deprecated_patterns(tmp_path, CATALOG)}

    assert rule_files == {("ts-namespace-module-keyword", "a.ts")}


def test_analysis_evidence_describes_a_node_project(tmp_path: Path) -> None:
    evidence = collect_analysis_evidence(node_project(tmp_path), CATALOG)

    assert evidence.languages[0] == "javascript"
    assert evidence.node_engine == ">=8"
    assert evidence.package_scripts == {"test": "jest"}
    assert "src/index.js" in evidence.entry_points
    assert evidence.tests.framework == "jest"
    assert evidence.tests.test_files == ["src/index.test.js"]
    assert evidence.tests.test_count == 2
    assert {item["name"] for item in evidence.dependencies} >= {"request", "express", "jest"}
    assert not any(path.startswith("node_modules/") for path in evidence.files)


def test_jest_and_vitest_json_reports_become_per_test_ids() -> None:
    payload = {
        "testResults": [
            {
                "name": "/work/src/index.test.js",
                "status": "failed",
                "assertionResults": [
                    {"fullName": "parser parses hosts", "status": "passed"},
                    {"fullName": "parser rejects junk", "status": "failed", "failureMessages": ["Error: expected 1\n at x"]},
                    {"fullName": "parser later", "status": "pending"},
                ],
            },
            {"name": "/work/src/broken.test.js", "status": "failed", "assertionResults": [], "message": "SyntaxError: x"},
        ]
    }

    report = TestRunner.parse_js_report(payload, framework="jest", phase="baseline", install_log_tail="", duration_s=1)

    assert report.passed == ["src/index.test.js::parser parses hosts"]
    assert report.skipped == ["src/index.test.js::parser later"]
    assert [item.test_id for item in report.failed] == ["src/index.test.js::parser rejects junk"]
    assert report.failed[0].file == "src/index.test.js"
    assert [item.test_id for item in report.errored] == ["src/broken.test.js::<suite>"]


def test_mocha_json_report_separates_hook_errors() -> None:
    payload = {
        "passes": [{"fullTitle": "api returns 200", "file": "/work/test/api.spec.js"}],
        "pending": [],
        "failures": [
            {"fullTitle": "api returns 404", "file": "/work/test/api.spec.js", "err": {"message": "AssertionError: 200 != 404"}},
            {"fullTitle": 'api "before all" hook for "returns 200"', "file": "/work/test/api.spec.js", "err": {"message": "boom"}},
        ],
    }

    report = TestRunner.parse_js_report(payload, framework="mocha", phase="baseline", install_log_tail="", duration_s=1)

    assert report.passed == ["test/api.spec.js::api returns 200"]
    assert [item.test_id for item in report.failed] == ["test/api.spec.js::api returns 404"]
    assert len(report.errored) == 1


def test_js_test_command_preserves_project_script_arguments() -> None:
    direct = TestRunner.js_test_command("jest", "jest --config jest.ci.js")
    wrapped = TestRunner.js_test_command("vitest", "npm run lint && vitest")

    assert "npm run test -- --ci --json" in direct
    assert "npx --no-install vitest run --run --reporter=json" in wrapped
    assert direct.startswith("mkdir -p .reforge-artifacts && CI=true ")


class RecordingSandbox:
    def __init__(self, workspace: Path, payload: dict[str, object] | None = None) -> None:
        self.workspace = workspace
        self.payload = payload
        self.commands: list[str] = []

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        command = cmd[-1]
        self.commands.append(command)
        if self.payload is not None and "--json" in command:
            artifact = self.workspace / ".reforge-artifacts" / "js-test-report.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(json.dumps(self.payload), encoding="utf-8")
        return ExecResult(0, "", "", 0.01, False)


def test_node_runner_freezes_individual_jest_tests(tmp_path: Path) -> None:
    root = node_project(tmp_path)
    payload = {"testResults": [{"name": f"{root.as_posix()}/src/index.test.js", "assertionResults": [{"fullName": "parses", "status": "passed"}]}]}
    sandbox = RecordingSandbox(root, payload)

    report = TestRunner(sandbox, root).run("baseline")

    assert report.install_ok
    assert report.passed[0].endswith("src/index.test.js::parses")
    assert "npm ci --no-audit --no-fund" in sandbox.commands[0]


def test_package_managers_use_their_lockfiles(tmp_path: Path) -> None:
    pnpm = write(tmp_path / "pnpm", {"package.json": "{}", "pnpm-lock.yaml": ""})
    yarn = write(tmp_path / "yarn", {"package.json": "{}", "yarn.lock": ""})

    assert "corepack pnpm install --frozen-lockfile" in TestRunner(RecordingSandbox(pnpm), pnpm).node_install_command()
    assert "corepack yarn install" in TestRunner(RecordingSandbox(yarn), yarn).node_install_command()


def test_polyglot_reports_merge_both_toolchains() -> None:
    python = StateTestReport(phase="baseline", install_ok=True, passed=["tests/test_a.py::test_x"], duration_s=1, lint_issues=2)
    node = StateTestReport(phase="baseline", install_ok=False, passed=["web/a.test.js::a"], duration_s=2, typecheck_errors=1)

    merged = TestRunner.merge_reports(python, node)

    assert merged.passed == ["tests/test_a.py::test_x", "web/a.test.js::a"]
    assert merged.install_ok is False
    assert merged.lint_issues == 2 and merged.typecheck_errors == 1
    assert merged.duration_s == 3


def test_generator_supports_jest_projects_and_enforces_js_paths(tmp_path: Path) -> None:
    root = node_project(tmp_path)
    generator = CharacterizationTestGenerator(executor=object())  # type: ignore[arg-type]

    assert generator.supports(root) is True
    proposal = GenerationProposal(
        summary="x", files=[GeneratedTestFile(path="tests/reforge_generated/index.test.js", content="it('x', () => {});")]
    )
    assert generator.write(root, proposal) == ["tests/reforge_generated/index.test.js"]
    with pytest.raises(CharacterizationPathError, match=".test.js"):
        generator._validate_paths(
            GenerationProposal(summary="x", files=[GeneratedTestFile(path="tests/reforge_generated/test_x.py", content="x")]),
            "javascript",
            ".test.js",
        )


def test_generator_uses_node_test_for_projects_without_a_test_framework(tmp_path: Path) -> None:
    write(tmp_path, {"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}}), "src/a.js": "x"})
    generator = CharacterizationTestGenerator(executor=object())  # type: ignore[arg-type]

    assert generator.supports(tmp_path) is True
    assert generator._generation_framework(tmp_path) == "node"
    assert generator._js_extension(tmp_path, "node") == ".test.js"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("src/app.js", False), ("src/app.spec.ts", True), ("tests/fixtures/data.json", True), ("src/testing/util.py", False)],
)
def test_test_infrastructure_classification(path: str, expected: bool) -> None:
    assert is_test_infrastructure(path) is expected


def test_tooling_only_pyproject_does_not_add_a_python_suite_to_a_node_project(tmp_path: Path) -> None:
    root = node_project(tmp_path, extra={"pyproject.toml": "[tool.black]\nline-length = 100\n"})
    sandbox = RecordingSandbox(root, {"testResults": []})

    TestRunner(sandbox, root).run("baseline")

    assert not any("pytest" in command or ".venv" in command for command in sandbox.commands)


def test_node_project_with_python_tests_runs_both_suites(tmp_path: Path) -> None:
    root = node_project(tmp_path, extra={"api/tests/test_health.py": "def test_ok():\n    assert True\n"})
    sandbox = RecordingSandbox(root, {"testResults": []})

    TestRunner(sandbox, root).run("baseline")

    assert any("pytest" in command for command in sandbox.commands)
    assert any("--json" in command and "pytest" not in command for command in sandbox.commands)
