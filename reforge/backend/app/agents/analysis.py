"""Analysis agent and deterministic evidence collection."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from pydantic import Field

from ..core.state import AnalysisReport, DependencyFinding, DeprecatedPattern, StrictModel, TestInventory
from ..tools.ast_tools import RuleCatalog, scan_android_deprecated_patterns, scan_deprecated_patterns
from ..tools.deps_tools import DependencyParser
from ..tools.repo_tools import RepositoryTools
from ..llm import LLMClient
from .base import StructuredAgent
from .crewai_adapter import CrewAIExecutor
from .prompts import ANALYSIS_PROMPT


class AnalysisEvidence(StrictModel):
    files: list[str]
    dependencies: list[dict[str, str | None]] = Field(default_factory=list)
    deprecated_patterns: list[DeprecatedPattern] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    tests: TestInventory


def collect_analysis_evidence(workspace: Path, catalog: RuleCatalog) -> AnalysisEvidence:
    """Collect read-only deterministic inputs for the LLM analysis step."""
    repository = RepositoryTools(workspace)
    files = repository.list_tree()
    dependencies = [
        {"name": dependency.name, "constraint": dependency.constraint, "source": dependency.source}
        for dependency in DependencyParser(workspace).parse()
    ]
    config_files = [
        path
        for path in files
        if Path(path).name in {"pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt"}
        or Path(path).name.startswith("requirements")
        or Path(path).name in {
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradle-wrapper.properties",
            "AndroidManifest.xml",
            "package.json",
            "package-lock.json",
        }
    ]
    entry_points = [path for path in files if Path(path).name in {"__main__.py", "app.py", "main.py"}]
    python_test_files = [path for path in files if Path(path).name.startswith("test_") and path.endswith(".py")]
    android_test_files = [
        path
        for path in files
        if ("/src/test/" in f"/{path}" or "/src/androidTest/" in f"/{path}")
        and path.endswith((".java", ".kt"))
    ]
    node_test_files = [
        path
        for path in files
        if path.endswith((".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx", ".test.js", ".spec.js"))
    ]
    package_scripts: dict[str, object] = {}
    package_json = workspace / "package.json"
    if package_json.is_file():
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
            candidate = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
            package_scripts = candidate if isinstance(candidate, dict) else {}
        except json.JSONDecodeError:
            pass
    test_files = [*python_test_files, *android_test_files, *node_test_files]
    framework = (
        "pytest"
        if python_test_files or "pytest.ini" in files
        else "JUnit/AndroidX" if android_test_files
        else "npm" if "test" in package_scripts else None
    )
    test_count = 0
    for relative in python_test_files:
        try:
            tree = ast.parse(repository.read_file(relative))
            test_count += sum(isinstance(node, ast.FunctionDef) and node.name.startswith("test_") for node in ast.walk(tree))
        except SyntaxError:
            continue
    for relative in android_test_files:
        try:
            test_count += repository.read_file(relative).count("@Test")
        except UnicodeDecodeError:
            continue
    for relative in node_test_files:
        try:
            test_count += repository.read_file(relative).count("test(") + repository.read_file(relative).count("it(")
        except UnicodeDecodeError:
            continue
    return AnalysisEvidence(
        files=files,
        dependencies=dependencies,
        deprecated_patterns=[
            *scan_deprecated_patterns(workspace.resolve(), catalog),
            *scan_android_deprecated_patterns(workspace.resolve()),
        ],
        config_files=config_files,
        entry_points=entry_points,
        tests=TestInventory(
            framework=framework,
            test_files=test_files,
            test_count=test_count,
            has_tests=bool(test_files),
        ),
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
        crew_executor: CrewAIExecutor | None = None,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (crew_executor is None):
            raise ValueError("provide exactly one of llm or crew_executor")
        self.crew_executor = crew_executor
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def run(self, objective: str, evidence: AnalysisEvidence) -> AnalysisReport:
        prompt = ANALYSIS_PROMPT.format(
            objective=objective,
            evidence=json.dumps(evidence.model_dump(mode="json")),
            schema=json.dumps(AnalysisReport.model_json_schema()),
        )
        if self.crew_executor is not None:
            report = self.crew_executor.run(
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
            deprecated = name.lower() == "com.nineoldandroids:library"
            findings.append(
                DependencyFinding(
                    name=name,
                    current=str(constraint) if constraint is not None else None,
                    target=None,
                    status="deprecated" if deprecated else "unpinned" if constraint is None else "ok",
                    reason=(
                        "NineOldAndroids is obsolete; replace it with Android platform or AndroidX APIs."
                        if deprecated
                        else f"Declared in {source}; no verified upgrade target was supplied."
                    ),
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
                "config_files": sorted(set([*report.config_files, *evidence.config_files])),
                "summary": f"{report.summary}{note}".strip(),
            }
        )
