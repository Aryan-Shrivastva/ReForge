from __future__ import annotations

import json
from pathlib import Path
from reforge.backend.app.core.state import (
    AnalysisReport,
    DeprecatedPattern,
    DependencyFinding,
    FinalStatus,
    Plan,
    PlanStep,
    Stage,
    TestInventory as StateTestInventory,
    TestReport as StateTestReport,
)
from reforge.backend.app.reporting import ReportGenerator
from reforge.backend.app.tools.ast_tools import RuleCatalog

from conftest import make_state


def test_report_generator_writes_markdown_and_json_with_required_sections() -> None:
    workspace = Path(".report-test") / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = make_state(
        workspace_path=str(workspace),
        stage=Stage.REPORT,
        final_status=FinalStatus.SUCCESS,
        analysis=AnalysisReport(
            tests=StateTestInventory(has_tests=True, test_count=1),
            dependencies=[DependencyFinding(name="flask", current="1.1", target=">=3", status="outdated", reason="upgrade")],
            deprecated_patterns=[DeprecatedPattern(rule_id="old-import", file="app.py", line=1, snippet="import old", replacement_hint="use new", source="ast_rule")],
            summary="analysis complete",
        ),
        plan=Plan(version=1, steps=[PlanStep(id="S1", title="Upgrade", description="Upgrade dependency", files=["requirements.txt"], kind="dependency", validation_hint="pytest", status="applied")], rationale="required"),
        baseline=StateTestReport(phase="baseline", install_ok=True, passed=["tests/test_app.py::test_ok"], duration_s=1),
        baseline_pass_set=["tests/test_app.py::test_ok"],
        test_runs=[StateTestReport(phase="post_transform", install_ok=True, passed=["tests/test_app.py::test_ok"], duration_s=1)],
    )

    artifacts = ReportGenerator(RuleCatalog([])).write(state)

    markdown = artifacts.markdown.read_text(encoding="utf-8")
    payload = json.loads(artifacts.json.read_text(encoding="utf-8"))
    assert "# ReForge modernization report" in markdown
    assert "## Reproduction" in markdown
    assert payload["outcome"]["status"] == "SUCCESS"
    assert payload["dependencies"][0]["name"] == "flask"
    assert payload["tests"]["baseline"]["passed"] == 1
