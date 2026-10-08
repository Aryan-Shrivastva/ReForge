from pathlib import Path

from reforge.backend.app.core.events import RunStateStore
from reforge.backend.app.core.state import (
    AnalysisReport,
    Budgets,
    Plan,
    PlanStep,
    RepairAttempt,
    TestInventory as StateTestInventory,
    TestReport as StateTestReport,
    TransformationResult,
)
from reforge.backend.app.core.workflow import WorkflowRunner
from reforge.backend.app.tools.ast_tools import RuleCatalog
from reforge.backend.tests.conftest import make_state


WORKSPACE = Path("reforge/backend/tests/fixtures/dependency_repo")


class FakeWorkflowGit:
    def __init__(self, _: Path) -> None:
        self.reset_revision: str | None = None

    def current_commit(self) -> str:
        return "a" * 40

    def create_branch(self, _: str) -> None:
        return None

    def commit(self, _: str, paths: list[str] | None = None) -> str:
        return "b" * 40

    def reset_to(self, revision: str) -> None:
        self.reset_revision = revision


class FakeAnalysisAgent:
    def run(self, objective: str, evidence: object) -> AnalysisReport:
        assert objective == "Modernize dependency"
        assert evidence is not None
        return AnalysisReport(
            tests=StateTestInventory(
                framework="pytest", test_files=["tests/test_legacy.py"], test_count=1, has_tests=True
            ),
            summary="Fixture analysis.",
        )


class FakePlanningAgent:
    def run(self, objective: str, analysis: AnalysisReport, workspace_files: list[str]) -> Plan:
        assert analysis.summary == "Fixture analysis."
        assert "requirements.txt" in workspace_files
        return Plan(
            version=1,
            rationale="One safe change.",
            steps=[
                PlanStep(
                    id="S1",
                    title="Modernize dependency",
                    description="Update the declared dependency.",
                    files=["requirements.txt"],
                    depends_on=[],
                    kind="dependency",
                    validation_hint="Tests pass.",
                )
            ],
        )


class FakeTransformationAgent:
    def __init__(self) -> None:
        self.protected: list[str] = []

    def run(self, **kwargs: object) -> TransformationResult:
        guards = kwargs["guards"]
        self.protected = sorted(guards.protected_test_files)  # type: ignore[union-attr]
        return TransformationResult(
            step_id="S1",
            status="applied",
            files_changed=["requirements.txt"],
            commit_sha="b" * 40,
            diff="--- a/requirements.txt\n+++ b/requirements.txt\n-old\n+new\n",
        )


class FakeTestingRunner:
    def __init__(self, *, baseline_passes: bool = True, post_passes: bool = True) -> None:
        self.baseline_passes = baseline_passes
        self.post_passes = post_passes
        self.phases: list[str] = []

    def run(self, phase: str, **_: object) -> StateTestReport:
        self.phases.append(phase)
        passed = (
            ["tests/test_legacy.py::test_behavior"]
            if (phase == "baseline" and self.baseline_passes) or (phase != "baseline" and self.post_passes)
            else []
        )
        return StateTestReport(
            phase=phase,  # type: ignore[arg-type]
            install_ok=True,
            passed=passed,
            duration_s=0.01,
            lint_issues=0,
        )


class RepairingTestingRunner(FakeTestingRunner):
    def run(self, phase: str, **_: object) -> StateTestReport:
        self.phases.append(phase)
        return StateTestReport(
            phase=phase,  # type: ignore[arg-type]
            install_ok=True,
            passed=["tests/test_legacy.py::test_behavior"] if phase in {"baseline", "post_repair"} else [],
            duration_s=0.01,
            lint_issues=0,
        )


class FakeRepairAgent:
    def run(self, **kwargs: object) -> RepairAttempt:
        return RepairAttempt(
            iteration=len(kwargs["repairs"]) + 1,  # type: ignore[arg-type]
            diagnosis="Repair fixture regression",
            root_cause_category="api_change",
            files_changed=["requirements.txt"],
            commit_sha="r" * 40,
            failing_before=kwargs["failing_before"],  # type: ignore[arg-type]
        )


def build_runner(testing: FakeTestingRunner, transformed: FakeTransformationAgent) -> WorkflowRunner:
    return WorkflowRunner(
        analysis_agent=FakeAnalysisAgent(),  # type: ignore[arg-type]
        planning_agent=FakePlanningAgent(),  # type: ignore[arg-type]
        transformation_agent=transformed,  # type: ignore[arg-type]
        testing_runner_factory=lambda _: testing,
        catalog=RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml")),
        state_store=RunStateStore(Path(".workflow-test-runs") / "replan"),
        min_baseline_passing_tests=1,
        git_factory=FakeWorkflowGit,  # type: ignore[arg-type]
    )


def test_m5_workflow_wires_agents_baseline_transform_test_and_persists() -> None:
    testing = FakeTestingRunner()
    transformed = FakeTransformationAgent()
    runner = build_runner(testing, transformed)
    state = make_state(
        run_id="workflow-success",
        objective="Modernize dependency",
        workspace_path=str(WORKSPACE.resolve()),
    )

    result = runner.run(state)

    assert result.stage.value == "DONE"
    assert result.final_status is not None and result.final_status.value == "SUCCESS"
    assert result.report_path is not None and Path(result.report_path).is_file()
    assert Path(result.report_path).with_suffix(".json").is_file()
    assert testing.phases == ["baseline", "post_transform"]
    assert transformed.protected == ["tests/test_legacy.py"]
    assert result.plan is not None and result.plan.steps[0].commit_sha == "b" * 40
    assert runner.state_store.load("workflow-success").final_status == result.final_status
    assert any(event.agent == "transformation" and event.kind == "result" for event in result.trace)


def test_m5_routes_unsuccessful_post_transform_run_to_failed_report() -> None:
    testing = FakeTestingRunner(post_passes=False)
    runner = build_runner(testing, FakeTransformationAgent())
    state = make_state(
        run_id="workflow-failure",
        objective="Modernize dependency",
        workspace_path=str(WORKSPACE.resolve()),
    )

    result = runner.run(state)

    assert result.stage.value == "DONE"
    assert result.final_status is not None and result.final_status.value == "FAILED"
    assert any("repair stage requires" in event.summary for event in result.trace)


def test_m5_fails_when_no_baseline_test_passes_and_still_persists() -> None:
    testing = FakeTestingRunner(baseline_passes=False)
    runner = build_runner(testing, FakeTransformationAgent())
    state = make_state(
        run_id="workflow-no-baseline",
        objective="Modernize dependency",
        workspace_path=str(WORKSPACE.resolve()),
    )

    result = runner.run(state)

    assert result.stage.value == "DONE"
    assert result.final_status is not None and result.final_status.value == "FAILED"
    assert testing.phases == ["baseline"]
    assert runner.state_store.load("workflow-no-baseline").metrics.wall_clock_s >= 0


def test_m6_repairs_a_failed_transform_and_retests() -> None:
    testing = RepairingTestingRunner()
    runner = build_runner(testing, FakeTransformationAgent())
    runner.repair_agent = FakeRepairAgent()  # type: ignore[assignment]
    state = make_state(
        run_id="workflow-repair", objective="Modernize dependency", workspace_path=str(WORKSPACE.resolve())
    )

    result = runner.run(state)

    assert result.final_status is not None and result.final_status.value == "SUCCESS"
    assert testing.phases == ["baseline", "post_transform", "post_repair"]
    assert len(result.repairs) == 1
    assert result.repairs[0].failing_after == 0


class ReplanningTestingRunner(FakeTestingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.post_runs = 0

    def run(self, phase: str, **_: object) -> StateTestReport:
        self.phases.append(phase)
        if phase == "baseline":
            passed = ["tests/test_legacy.py::test_behavior"]
        elif phase == "post_repair":
            passed = []
        else:
            self.post_runs += 1
            passed = [] if self.post_runs == 1 else ["tests/test_legacy.py::test_behavior"]
        return StateTestReport(phase=phase, install_ok=True, passed=passed, duration_s=0.01, lint_issues=0)


class ReplanningAgent(FakePlanningAgent):
    def replan(self, *args: object) -> Plan:
        plan = self.run(args[0], args[1], args[2])  # type: ignore[arg-type]
        plan.version = 2
        plan.rationale = "The first plan missed the compatibility layer."
        return plan


def test_m6_replans_after_bounded_non_progress() -> None:
    testing = ReplanningTestingRunner()
    runner = WorkflowRunner(
        analysis_agent=FakeAnalysisAgent(),  # type: ignore[arg-type]
        planning_agent=ReplanningAgent(),  # type: ignore[arg-type]
        transformation_agent=FakeTransformationAgent(),  # type: ignore[arg-type]
        repair_agent=FakeRepairAgent(),  # type: ignore[arg-type]
        testing_runner_factory=lambda _: testing,
        catalog=RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml")),
        state_store=RunStateStore(Path(".workflow-test-runs") / "replan-case"),
        min_baseline_passing_tests=1,
        git_factory=FakeWorkflowGit,  # type: ignore[arg-type]
    )
    state = make_state(
        run_id="workflow-replan", objective="Modernize dependency", workspace_path=str(WORKSPACE.resolve()),
        budgets=Budgets(max_no_progress=1),
    )

    result = runner.run(state)

    assert result.final_status is not None and result.final_status.value == "SUCCESS"
    assert result.replans == 1
    assert result.plan is not None and result.plan.version == 2
    assert testing.phases == ["baseline", "post_transform", "post_repair", "post_transform"]


def test_generated_baseline_file_must_contain_a_collected_passing_test() -> None:
    report = StateTestReport(
        phase="baseline", install_ok=True, duration_s=0,
        passed=["tests/test_legacy.py::test_behavior"],
    )
    collected = StateTestReport(
        phase="baseline", install_ok=True, duration_s=0,
        passed=["tests/reforge_generated/test_characterization.py::test_current_behavior"],
    )

    assert WorkflowRunner._generated_test_passes(report) == []
    assert WorkflowRunner._generated_test_passes(collected) == [
        "tests/reforge_generated/test_characterization.py::test_current_behavior"
    ]
