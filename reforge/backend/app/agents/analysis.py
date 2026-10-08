"""Analysis agent and deterministic evidence collection."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from pydantic import Field

from ..core.state import AnalysisReport, DeprecatedPattern, StrictModel, TestInventory
from ..tools.ast_tools import RuleCatalog, scan_deprecated_patterns
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
    ]
    entry_points = [path for path in files if Path(path).name in {"__main__.py", "app.py", "main.py"}]
    test_files = [path for path in files if Path(path).name.startswith("test_") and path.endswith(".py")]
    framework = "pytest" if test_files or "pytest.ini" in files else None
    test_count = 0
    for relative in test_files:
        try:
            tree = ast.parse(repository.read_file(relative))
            test_count += sum(isinstance(node, ast.FunctionDef) and node.name.startswith("test_") for node in ast.walk(tree))
        except SyntaxError:
            continue
    return AnalysisEvidence(
        files=files,
        dependencies=dependencies,
        deprecated_patterns=scan_deprecated_patterns(workspace.resolve(), catalog),
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
            return self.crew_executor.run(
                role="Repository modernization analyst",
                goal="Produce an evidence-grounded modernization analysis.",
                backstory="You interpret deterministic repository facts and never modify files.",
                prompt=prompt,
                expected_output="One AnalysisReport JSON object matching the requested schema.",
                output_model=AnalysisReport,
            )
        return self.complete_model(prompt, AnalysisReport)
