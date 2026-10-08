import json
from pathlib import Path

from reforge.backend.app.agents.analysis import AnalysisAgent, collect_analysis_evidence
from reforge.backend.app.agents.planning import PlanningAgent
from reforge.backend.app.core.state import AnalysisReport
from reforge.backend.app.tools.ast_tools import RuleCatalog


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def analysis_payload() -> dict[str, object]:
    return {
        "python_version_current": "3.8",
        "python_version_target": "3.11",
        "dependencies": [],
        "deprecated_patterns": [],
        "config_files": ["requirements.txt"],
        "affected_files": ["legacy.py"],
        "entry_points": ["main.py"],
        "tests": {"framework": "pytest", "test_files": ["tests/test_a.py"], "test_count": 1, "has_tests": True},
        "risks": ["API compatibility"],
        "summary": "A valid fake analysis.",
    }


def valid_plan() -> dict[str, object]:
    return {
        "version": 1,
        "steps": [
            {
                "id": "S1",
                "title": "Update dependency",
                "description": "Update the declared requirement.",
                "files": ["requirements.txt"],
                "depends_on": [],
                "kind": "dependency",
                "validation_hint": "Dependencies resolve.",
            },
            {
                "id": "S2",
                "title": "Replace API",
                "description": "Replace old API usage.",
                "files": ["legacy.py"],
                "depends_on": ["S1"],
                "kind": "api_replacement",
                "validation_hint": "Tests pass.",
            },
        ],
        "rationale": "Dependencies must be updated before source usage.",
    }


def test_analysis_agent_retries_invalid_json() -> None:
    catalog = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))
    evidence = collect_analysis_evidence(Path("reforge/backend/tests/fixtures/dependency_repo"), catalog)
    llm = FakeLLM(["not valid json", json.dumps(analysis_payload())])

    report = AnalysisAgent(llm).run("Upgrade dependencies", evidence)

    assert report.summary == "A valid fake analysis."
    assert len(llm.prompts) == 2
    assert "previous response was invalid" in llm.prompts[1]
    assert "parse_dependencies" in AnalysisAgent.tool_names


def test_planning_agent_retries_a_cyclic_plan() -> None:
    cycle = valid_plan()
    cycle["steps"][0]["depends_on"] = ["S2"]  # type: ignore[index]
    llm = FakeLLM([json.dumps(cycle), json.dumps(valid_plan())])
    analysis = AnalysisReport.model_validate(analysis_payload())

    plan = PlanningAgent(llm).run("Upgrade dependencies", analysis, ["requirements.txt", "legacy.py"])

    assert [step.id for step in plan.steps] == ["S1", "S2"]
    assert len(llm.prompts) == 2
    assert "cycle" in llm.prompts[1]
