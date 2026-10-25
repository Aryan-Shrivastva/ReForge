"""Workflow fixes: settings budgets, token budget, honest TEST routing, scoped success, ADK session sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reforge.backend.app.agents.executor import AgentExecutors, TokenUsage
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.events import RunStateStore
from reforge.backend.app.core.state import (
    AnalysisReport,
    DeprecatedPattern,
    Failure,
    Plan,
    PlanStep,
    TestInventory as StateTestInventory,
    TestReport as StateTestReport,
    TransformationResult,
)
from reforge.backend.app.core.workflow import WorkflowRunner, budgets_from_settings
from reforge.backend.app.tools.ast_tools import RuleCatalog
from reforge.backend.tests.conftest import make_state
from reforge.backend.tests.test_workflow import (
    FakeAnalysisAgent,
    FakePlanningAgent,
    FakeRepairAgent,
    FakeTransformationAgent,
    FakeWorkflowGit,
    isolated_workspace,
)

CATALOG = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))
BASELINE_TEST = "tests/test_legacy.py::test_behavior"


class TwoStepPlanningAgent(FakePlanningAgent):
    def run(self, objective: str, analysis: AnalysisReport, workspace_files: list[str]) -> Plan:
        plan = super().run(objective, analysis, workspace_files)
        plan.steps.append(
            PlanStep(
                id="S2", title="Second", description="Second step.", files=["setup.py"], depends_on=[],
                kind="api_replacement", validation_hint="Tests pass.",
            )
        )
        return plan


class FirstStepOnlyTransformation(FakeTransformationAgent):
    """S1 applies; S2 fails (e.g. its patch did not apply)."""

    def run(self, **kwargs: object) -> TransformationResult:
        step = kwargs["step"]
        if step.id == "S1":  # type: ignore[union-attr]
            return super().run(**kwargs)
        return TransformationResult(step_id="S2", status="failed", error="patch rejected")


class ScriptedTestingRunner:
    def __init__(self, reports: dict[str, list[StateTestReport]]) -> None:
        self.reports = reports
        self.phases: list[str] = []

    def run(self, phase: str, **_: object) -> StateTestReport:
        self.phases.append(phase)
        return self.reports[phase].pop(0)


def report(phase: str, passed: list[str], *, errored: list[str] | None = None) -> StateTestReport:
    return StateTestReport(
        phase=phase,  # type: ignore[arg-type]
        install_ok=True,
        passed=passed,
        errored=[Failure(test_id=item, error_type="E", message="m", traceback_excerpt="") for item in errored or []],
        duration_s=0.01,
        lint_issues=0,
    )


def runner(testing: ScriptedTestingRunner, *, planning=None, transformation=None, store: str, **kwargs: Any) -> WorkflowRunner:
    return WorkflowRunner(
        analysis_agent=FakeAnalysisAgent(),  # type: ignore[arg-type]
        planning_agent=planning or FakePlanningAgent(),  # type: ignore[arg-type]
        transformation_agent=transformation or FakeTransformationAgent(),  # type: ignore[arg-type]
        testing_runner_factory=lambda _: testing,
        catalog=CATALOG,
        state_store=RunStateStore(Path(".workflow-test-runs") / store),
        min_baseline_passing_tests=1,
        git_factory=FakeWorkflowGit,  # type: ignore[arg-type]
        **kwargs,
    )


def test_settings_budgets_are_applied_to_new_runs(tmp_path: Path) -> None:
    settings = Settings(max_repairs=9, max_no_progress=3, max_replans=4, max_tokens=1234, max_wall_clock_s=99)
    workflow = runner(ScriptedTestingRunner({}), store="budgets", default_budgets=budgets_from_settings(settings))

    state = workflow.initialize(
        run_id="budgets", repo_source="x", objective="Modernize dependency", workspace=isolated_workspace(tmp_path)
    )

    assert state.budgets.model_dump() == {
        "max_repairs": 9, "max_no_progress": 3, "max_replans": 4, "max_tokens": 1234, "max_wall_clock_s": 99
    }


def test_regression_with_unapplied_steps_goes_to_repair_not_partial(tmp_path: Path) -> None:
    testing = ScriptedTestingRunner({
        "baseline": [report("baseline", [BASELINE_TEST])],
        "post_transform": [report("post_transform", [])],
        "post_repair": [report("post_repair", [BASELINE_TEST])],
    })
    workflow = runner(
        testing, planning=TwoStepPlanningAgent(), transformation=FirstStepOnlyTransformation(), store="regression",
        repair_agent=FakeRepairAgent(),
    )
    state = make_state(run_id="regression", objective="Modernize dependency", workspace_path=str(isolated_workspace(tmp_path)))

    result = workflow.run(state)

    # Old behaviour: PARTIAL "baseline verification passed" straight after the
    # regressing post_transform run.  Now the regression is repaired first.
    assert testing.phases == ["baseline", "post_transform", "post_repair"]
    assert len(result.repairs) == 1
    assert result.final_status is not None and result.final_status.value == "PARTIAL"
    assert any(event.data.get("unapplied_steps") == ["S2"] for event in result.trace)
    assert any("regressed the frozen baseline" in event.summary for event in result.trace)


def test_partial_is_reported_only_when_the_baseline_actually_holds(tmp_path: Path) -> None:
    testing = ScriptedTestingRunner({
        "baseline": [report("baseline", [BASELINE_TEST])],
        "post_transform": [report("post_transform", [BASELINE_TEST])],
    })
    workflow = runner(testing, planning=TwoStepPlanningAgent(), transformation=FirstStepOnlyTransformation(), store="partial")
    state = make_state(run_id="partial", objective="Modernize dependency", workspace_path=str(isolated_workspace(tmp_path)))

    result = workflow.run(state)

    assert result.final_status is not None and result.final_status.value == "PARTIAL"
    assert testing.phases == ["baseline", "post_transform"]
    assert not result.repairs


def test_pre_existing_errors_do_not_block_success_but_new_errors_do(tmp_path: Path) -> None:
    testing = ScriptedTestingRunner({
        "baseline": [report("baseline", [BASELINE_TEST], errored=["tests/test_env.py::test_needs_db"])],
        "post_transform": [report("post_transform", [BASELINE_TEST], errored=["tests/test_env.py::test_needs_db"])],
    })
    workflow = runner(testing, store="errors")
    state = make_state(run_id="errors", objective="Modernize dependency", workspace_path=str(isolated_workspace(tmp_path)))

    assert workflow.run(state).final_status.value == "SUCCESS"  # type: ignore[union-attr]

    regressed = make_state(baseline_pass_set=[BASELINE_TEST], baseline=report("baseline", [BASELINE_TEST]))
    assert WorkflowRunner._regressions(regressed, report("post_transform", [BASELINE_TEST], errored=["new::err"])) == ["new::err"]


def test_success_ignores_out_of_scope_patterns_but_not_targeted_or_new_ones(tmp_path: Path) -> None:
    workspace = isolated_workspace(tmp_path)
    (workspace / "other.py").write_text("import imp\n", encoding="utf-8")
    workflow = runner(ScriptedTestingRunner({}), store="scope")
    existing = DeprecatedPattern(
        rule_id="imp-module", file="other.py", line=1, snippet="import imp", replacement_hint="h", source="ast_rule"
    )
    state = make_state(
        analysis=AnalysisReport(tests=StateTestInventory(), summary="s", deprecated_patterns=[existing]),
        plan=Plan(version=1, rationale="r", steps=[
            PlanStep(id="S1", title="t", description="d", files=["requirements.txt"], kind="dependency",
                     validation_hint="v", status="applied")
        ]),
    )

    assert workflow.remaining_targeted_patterns(state, workspace) == []

    state.plan.steps[0].files.append("other.py")  # type: ignore[union-attr]
    assert [item.file for item in workflow.remaining_targeted_patterns(state, workspace)] == ["other.py"]

    state.plan.steps[0].files.remove("other.py")  # type: ignore[union-attr]
    (workspace / "introduced.py").write_text("import distutils\n", encoding="utf-8")
    assert [item.file for item in workflow.remaining_targeted_patterns(state, workspace)] == ["introduced.py"]


@dataclass
class MeteredExecutor:
    framework: str = "crewai"
    usage: TokenUsage = field(default_factory=TokenUsage)


class TokenHungryAnalysis(FakeAnalysisAgent):
    def __init__(self, executor: MeteredExecutor) -> None:
        self.executor = executor

    def run(self, objective: str, evidence: object) -> AnalysisReport:
        self.executor.usage.add(900, 200)
        return super().run(objective, evidence)


def test_token_budget_is_tracked_and_aborts_the_run(tmp_path: Path) -> None:
    metered = MeteredExecutor()
    executors = AgentExecutors(metered, metered, metered, metered, metered)  # type: ignore[arg-type]
    workflow = runner(ScriptedTestingRunner({}), store="tokens", executors=executors)
    workflow.analysis_agent = TokenHungryAnalysis(metered)  # type: ignore[assignment]
    state = workflow.initialize(
        run_id="tokens", repo_source="x", objective="Modernize dependency", workspace=isolated_workspace(tmp_path),
    )
    state.budgets.max_tokens = 1_000

    result = workflow.run(state)

    assert result.metrics.tokens_in == 900 and result.metrics.tokens_out == 200
    assert result.final_status is not None and result.final_status.value == "ABORTED"
    assert any(event.summary == "Token budget exceeded." for event in result.trace)
    assert result.agent_runtime["analysis"] == "crewai"


@dataclass
class RecordingSessionExecutor:
    framework: str = "adk"
    usage: TokenUsage = field(default_factory=TokenUsage)
    bound: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    deltas: list[dict[str, Any]] = field(default_factory=list)

    def bind_run(self, run_id: str, initial_state: dict[str, Any]) -> None:
        self.bound.append((run_id, initial_state))

    def update_run_state(self, delta: dict[str, Any], *, author: str = "reforge_orchestrator") -> None:
        self.deltas.append(delta)

    def run_state(self) -> dict[str, Any]:
        return {"stage": self.deltas[-1]["stage"] if self.deltas else "INIT"}


def test_adk_session_receives_initial_state_and_stage_updates(tmp_path: Path) -> None:
    session = RecordingSessionExecutor()
    crew = MeteredExecutor()
    executors = AgentExecutors(session, session, crew, crew, crew)  # type: ignore[arg-type]
    testing = ScriptedTestingRunner({
        "baseline": [report("baseline", [BASELINE_TEST])],
        "post_transform": [report("post_transform", [BASELINE_TEST])],
    })
    workflow = runner(testing, store="session", executors=executors)

    state = workflow.initialize(
        run_id="session-run", repo_source="repo.zip", objective="Modernize dependency", workspace=isolated_workspace(tmp_path)
    )
    result = workflow.run(state)

    assert session.bound[0][0] == "session-run"
    assert session.bound[0][1]["objective"] == "Modernize dependency"
    stages = [delta["stage"] for delta in session.deltas]
    assert {"ANALYZE", "PLAN", "BASELINE", "TRANSFORM", "TEST", "DONE"} <= set(stages)
    assert any("S1 [applied]" in delta["plan_summary"] for delta in session.deltas)
    assert result.agent_session == {"stage": "DONE"}
    assert result.agent_runtime == {
        "analysis": "adk", "planning": "adk", "transformation": "crewai", "repair": "crewai", "test_generation": "crewai"
    }
