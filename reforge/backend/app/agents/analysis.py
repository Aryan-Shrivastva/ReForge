"""Analysis agent and deterministic evidence collection."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from pydantic import Field

from ..core.state import AnalysisReport, DependencyFinding, DeprecatedPattern, StrictModel, TestInventory
from ..tools.ast_tools import RuleCatalog, scan_all_deprecated_patterns
from ..tools.deps_tools import DependencyParser
from ..tools.languages import (
    detect_js_test_framework,
    detect_languages,
    is_js_test_file,
    is_python_test_file,
    is_skipped,
    is_test_file,
    package_scripts,
    read_package_json,
)
from ..tools.repo_tools import RepositoryTools
from ..llm import LLMClient
from .base import StructuredAgent
from .executor import AgentExecutor
from .prompts import ANALYSIS_PROMPT


class AnalysisEvidence(StrictModel):
    files: list[str]
    languages: list[str] = Field(default_factory=list)
    dependencies: list[dict[str, str | None]] = Field(default_factory=list)
    deprecated_patterns: list[DeprecatedPattern] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    tests: TestInventory
    node_engine: str | None = None
    package_scripts: dict[str, str] = Field(default_factory=dict)


_CONFIG_FILE_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "requirements.txt",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle-wrapper.properties",
        "AndroidManifest.xml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "jsconfig.json",
        ".babelrc",
        "babel.config.js",
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.json",
        "eslint.config.js",
        "eslint.config.mjs",
        ".nvmrc",
        ".node-version",
        "jest.config.js",
        "jest.config.ts",
        "vitest.config.ts",
        "vitest.config.js",
        "vite.config.ts",
        "vite.config.js",
        "webpack.config.js",
    }
)
_PYTHON_ENTRY_POINTS = frozenset({"__main__.py", "app.py", "main.py", "manage.py", "wsgi.py", "asgi.py"})
_JS_ENTRY_POINTS = frozenset({"index.js", "index.ts", "server.js", "server.ts", "app.js", "app.ts", "main.js", "main.ts"})
# Counts JS test cases: it(...), test(...), and their .each forms, not suite blocks.
_JS_TEST_CASE = re.compile(r"(?<![\w.$])(?:it|test)(?:\.each\s*(?:\([^)]*\)|`[^`]*`))?\s*\(")


# Packages whose maintainers have deprecated them.  Reported deterministically
# so a model cannot omit them; no replacement version is fabricated.
KNOWN_DEPRECATED_PACKAGES: dict[str, str] = {
    "com.nineoldandroids:library": "NineOldAndroids is obsolete; replace it with Android platform or AndroidX APIs.",
    "request": "The request package is deprecated; migrate to the global fetch API, undici, or axios.",
    "request-promise": "request-promise is deprecated with request; migrate to fetch, undici, or axios.",
    "request-promise-native": "request-promise-native is deprecated with request; migrate to fetch, undici, or axios.",
    "node-sass": "node-sass (LibSass) is deprecated; migrate to the sass (Dart Sass) package.",
    "tslint": "TSLint is deprecated; migrate to ESLint with typescript-eslint.",
    "babel-eslint": "babel-eslint is deprecated; use @babel/eslint-parser.",
    "eslint-loader": "eslint-loader is deprecated; use eslint-webpack-plugin.",
    "istanbul": "istanbul is deprecated; use nyc or c8.",
    "querystring": "The querystring npm package is deprecated; use URLSearchParams.",
    "crypto": "The crypto npm package is deprecated; use Node's built-in node:crypto module.",
    "@babel/polyfill": "@babel/polyfill is deprecated; import core-js/stable and regenerator-runtime directly.",
    "uglify-es": "uglify-es is unmaintained; use terser.",
    "gulp-util": "gulp-util is deprecated; use the individual replacement modules.",
}


def _package_entry_points(package: dict[str, object]) -> list[str]:
    entries: list[str] = []
    for key in ("main", "module", "types"):
        value = package.get(key)
        if isinstance(value, str):
            entries.append(value.removeprefix("./"))
    binaries = package.get("bin")
    if isinstance(binaries, str):
        entries.append(binaries.removeprefix("./"))
    elif isinstance(binaries, dict):
        entries.extend(value.removeprefix("./") for value in binaries.values() if isinstance(value, str))
    return entries


def collect_analysis_evidence(workspace: Path, catalog: RuleCatalog) -> AnalysisEvidence:
    """Collect read-only deterministic inputs for the LLM analysis step."""
    repository = RepositoryTools(workspace)
    files = [path for path in repository.list_tree() if not is_skipped(path)]
    languages = detect_languages(files)
    dependencies = [
        {"name": dependency.name, "constraint": dependency.constraint, "source": dependency.source}
        for dependency in DependencyParser(workspace).parse()
    ]
    config_files = [
        path
        for path in files
        if Path(path).name in _CONFIG_FILE_NAMES or Path(path).name.startswith("requirements")
    ]
    package = read_package_json(workspace)
    scripts = package_scripts(package)
    engines = package.get("engines")
    node_engine = str(engines["node"]) if isinstance(engines, dict) and isinstance(engines.get("node"), str) else None
    entry_points = sorted(
        {
            *[path for path in files if Path(path).name in _PYTHON_ENTRY_POINTS],
            *[path for path in files if Path(path).name in _JS_ENTRY_POINTS and path.count("/") <= 1],
            *[path for path in _package_entry_points(package) if path in files],
        }
    )
    python_test_files = [path for path in files if is_python_test_file(path)]
    js_test_files = [path for path in files if is_js_test_file(path)]
    android_test_files = [path for path in files if is_test_file(path) and path.endswith((".java", ".kt"))]
    test_files = [*python_test_files, *js_test_files, *android_test_files]

    js_framework = detect_js_test_framework(workspace) if package else None
    framework = (
        "pytest" if python_test_files or "pytest.ini" in files
        else js_framework if js_framework and (js_test_files or "test" in scripts)
        else "JUnit/AndroidX" if android_test_files
        else "npm" if "test" in scripts and not scripts["test"].startswith("echo")
        else None
    )
    test_count = 0
    for relative in python_test_files:
        try:
            tree = ast.parse(repository.read_file(relative))
            test_count += sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                for node in ast.walk(tree)
            )
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    for relative in android_test_files:
        try:
            test_count += repository.read_file(relative).count("@Test")
        except UnicodeDecodeError:
            continue
    for relative in js_test_files:
        try:
            test_count += len(_JS_TEST_CASE.findall(repository.read_file(relative)))
        except UnicodeDecodeError:
            continue
    return AnalysisEvidence(
        files=files,
        languages=list(languages),
        dependencies=dependencies,
        deprecated_patterns=scan_all_deprecated_patterns(workspace.resolve(), catalog),
        config_files=config_files,
        entry_points=entry_points,
        tests=TestInventory(
            framework=framework,
            test_files=test_files,
            test_count=test_count,
            has_tests=bool(test_files),
        ),
        node_engine=node_engine,
        package_scripts=scripts,
    )


class AnalysisAgent(StructuredAgent):
    """Turn deterministic evidence into a report through CrewAI or a test double."""

    tool_names = (
        "list_tree",
        "read_file",
        "search_code",
        "parse_dependencies",
        "scan_deprecated_patterns",
        "run_with_warnings",
        "query_latest_versions",
        "check_resolution",
        "detect_tests",
    )

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        executor: AgentExecutor | None = None,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (executor is None):
            raise ValueError("provide exactly one of llm or executor")
        self.executor = executor
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def run(self, objective: str, evidence: AnalysisEvidence) -> AnalysisReport:
        prompt = ANALYSIS_PROMPT.format(
            objective=objective,
            evidence=json.dumps(evidence.model_dump(mode="json")),
            schema=json.dumps(AnalysisReport.model_json_schema()),
        )
        if self.executor is not None:
            report = self.executor.run(
                role="Repository modernization analyst",
                goal="Produce an evidence-grounded modernization analysis.",
                backstory="You interpret deterministic repository facts and never modify files.",
                prompt=prompt,
                expected_output="One AnalysisReport JSON object matching the requested schema.",
                output_model=AnalysisReport,
            )
        else:
            report = self.complete_model(prompt, AnalysisReport)
        return self._reconcile_evidence(report, evidence)

    @staticmethod
    def _reconcile_evidence(report: AnalysisReport, evidence: AnalysisEvidence) -> AnalysisReport:
        """Keep deterministic discovery visible when a model omits it.

        The model supplies prioritisation and prose; it must not be able to
        erase declared dependencies, detected tests, or static rule matches.
        No upgrade target is fabricated during this reconciliation.
        """
        by_name = {item.name.lower(): item for item in report.dependencies}
        findings = list(report.dependencies)
        for dependency in evidence.dependencies:
            name = str(dependency["name"])
            if name.lower() in by_name:
                continue
            constraint = dependency.get("constraint")
            source = str(dependency["source"])
            deprecated_reason = KNOWN_DEPRECATED_PACKAGES.get(name.lower())
            findings.append(
                DependencyFinding(
                    name=name,
                    current=str(constraint) if constraint is not None else None,
                    target=None,
                    status="deprecated" if deprecated_reason else "unpinned" if constraint is None else "ok",
                    reason=deprecated_reason or f"Declared in {source}; no verified upgrade target was supplied.",
                )
            )
        known_patterns = {(item.rule_id, item.file, item.line) for item in report.deprecated_patterns}
        patterns = [
            *report.deprecated_patterns,
            *[
                item
                for item in evidence.deprecated_patterns
                if (item.rule_id, item.file, item.line) not in known_patterns
            ],
        ]
        note = ""
        if evidence.dependencies and len(report.dependencies) != len(findings):
            note += f" Deterministic discovery found {len(evidence.dependencies)} declared dependency entries."
        if evidence.tests.has_tests and not report.tests.has_tests:
            note += f" Deterministic discovery found {len(evidence.tests.test_files)} test file(s)."
        return report.model_copy(
            update={
                "dependencies": findings,
                "deprecated_patterns": patterns,
                "tests": evidence.tests if evidence.tests.has_tests else report.tests,
                "languages": evidence.languages or report.languages,
                "config_files": sorted(set([*report.config_files, *evidence.config_files])),
                "summary": f"{report.summary}{note}".strip(),
            }
        )
