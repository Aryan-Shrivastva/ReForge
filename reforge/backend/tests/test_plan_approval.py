"""Human-in-the-loop: runs pause after planning; nothing changes until a person decides."""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from reforge.backend.app.api.runs import RunCreateRequest, RunService
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.events import RunStateStore
from reforge.backend.app.core.state import AnalysisReport, FinalStatus, Plan, PlanStep, ProjectState, Stage
from reforge.backend.app.core.workflow import WorkflowRunner
from reforge.backend.app.main import create_app
from reforge.backend.app.tools.ast_tools import RuleCatalog
from reforge.backend.tests.conftest import make_state
from reforge.backend.tests.test_workflow import (
    FakeAnalysisAgent,
    FakeTestingRunner,
    FakeTransformationAgent,
    FakeWorkflowGit,
    isolated_workspace,
)

CATALOG = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))


def step(step_id: str, depends_on: list[str] | None = None) -> PlanStep:
    return PlanStep(
        id=step_id, title=f"Change {step_id}", description="d", files=["requirements.txt"],
        depends_on=depends_on or [], kind="dependency", validation_hint="tests pass",
    )


class RecordingPlanner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, objective: str, analysis: AnalysisReport, workspace_files: list[str], **kwargs: object) -> Plan:
        self.calls.append(kwargs)
        return Plan(version=1, rationale="r", steps=[step("S1"), step("S2", ["S1"]), step("S3")])


class CountingTransformation(FakeTransformationAgent):
    def __init__(self) -> None:
        super().__init__()
        self.applied: list[str] = []

    def run(self, **kwargs: object):
        current = kwargs["step"]
        self.applied.append(current.id)  # type: ignore[union-attr]
        result = super().run(**kwargs)
        return result.model_copy(update={"step_id": current.id})  # type: ignore[union-attr]


def make_runner(tmp_path: Path, planner: RecordingPlanner, transformation: CountingTransformation, testing: FakeTestingRunner) -> WorkflowRunner:
    return WorkflowRunner(
        analysis_agent=FakeAnalysisAgent(),  # type: ignore[arg-type]
        planning_agent=planner,  # type: ignore[arg-type]
        transformation_agent=transformation,  # type: ignore[arg-type]
        testing_runner_factory=lambda _: testing,
        catalog=CATALOG,
        state_store=RunStateStore(tmp_path / "store"),
        min_baseline_passing_tests=1,
        git_factory=FakeWorkflowGit,  # type: ignore[arg-type]
        require_plan_approval=True,
    )


def paused_run(tmp_path: Path):
    planner, transformation, testing = RecordingPlanner(), CountingTransformation(), FakeTestingRunner()
    runner = make_runner(tmp_path, planner, transformation, testing)
    state = make_state(run_id="approval", objective="Modernize dependency", workspace_path=str(isolated_workspace(tmp_path)), requires_approval=True)
    return runner, runner.run(state), planner, transformation, testing


def test_run_pauses_for_approval_before_changing_any_code(tmp_path: Path) -> None:
    _, state, _, transformation, testing = paused_run(tmp_path)

    assert state.stage is Stage.AWAIT_APPROVAL
    assert state.final_status is None
    assert transformation.applied == []
    assert testing.phases == ["baseline"]  # baseline recorded, nothing transformed
    assert "waiting for a person" in state.trace[-1].summary


def test_partial_approval_declines_dependents_and_applies_only_approved_steps(tmp_path: Path) -> None:
    runner, state, _, transformation, _ = paused_run(tmp_path)

    WorkflowRunner.apply_review(state, "approved", approved_steps=["S2", "S3"])
    # S2 depends on S1, which was not approved, so S2 is declined too.
    assert {item.id: item.status for item in state.plan.steps} == {"S1": "declined", "S2": "declined", "S3": "pending"}  # type: ignore[union-attr]
    finished = runner.run(state)

    assert transformation.applied == ["S3"]
    assert finished.final_status is FinalStatus.SUCCESS  # declined steps are a choice, not a failure
    assert finished.reviews[-1].approved_steps == ["S3"]
    assert finished.reviews[-1].declined_steps == ["S1", "S2"]


def test_requested_changes_go_back_to_the_planner_and_pause_again(tmp_path: Path) -> None:
    runner, state, planner, transformation, testing = paused_run(tmp_path)

    WorkflowRunner.apply_review(state, "revision_requested", feedback="Do not touch requirements.txt yet")
    revised = runner.run(state)

    assert revised.stage is Stage.AWAIT_APPROVAL
    assert planner.calls[-1]["reviewer_feedback"] == "Do not touch requirements.txt yet"
    assert revised.plan is not None and revised.plan.version == 2
    assert testing.phases == ["baseline"]  # baseline is reused, not re-run
    assert transformation.applied == []


def test_rejected_plan_ends_the_run_with_no_changes_and_a_report(tmp_path: Path) -> None:
    runner, state, _, transformation, _ = paused_run(tmp_path)

    WorkflowRunner.apply_review(state, "rejected", feedback="Not now")
    finished = runner.run(state)

    assert finished.final_status is FinalStatus.ABORTED
    assert finished.stage is Stage.DONE and finished.report_path
    assert transformation.applied == []


@pytest.mark.parametrize(
    ("decision", "kwargs", "message"),
    [
        ("approved", {"approved_steps": ["S9"]}, "unknown plan steps"),
        ("approved", {"approved_steps": []}, "at least one step"),
        ("revision_requested", {"feedback": "  "}, "describe the changes"),
        ("merge", {}, "unknown review decision"),
    ],
)
def test_invalid_reviews_are_rejected(decision: str, kwargs: dict[str, object], message: str) -> None:
    state = make_state(stage=Stage.AWAIT_APPROVAL, plan=Plan(version=1, rationale="r", steps=[step("S1")]))

    with pytest.raises(ValueError, match=message):
        WorkflowRunner.apply_review(state, decision, **kwargs)  # type: ignore[arg-type]


class PausingWorkflow:
    """Mimics the real workflow's pause/resume contract for the API."""

    def __init__(self, runs_root: Path) -> None:
        self.store = RunStateStore(runs_root)
        self.cancel_checker = None

    def initialize(self, *, run_id: str, repo_source: str, objective: str, workspace: Path) -> ProjectState:
        state = ProjectState(run_id=run_id, repo_source=repo_source, objective=objective, workspace_path=str(workspace),
                             work_branch=f"reforge/{run_id}", base_commit="a" * 40, requires_approval=True)
        self.store.save(state)
        return state

    def run(self, state: ProjectState) -> ProjectState:
        if state.stage is Stage.INIT:
            state.plan = Plan(version=1, rationale="r", steps=[step("S1")])
            state.stage = Stage.AWAIT_APPROVAL
        elif state.stage is Stage.TRANSFORM:
            state.plan.steps[0].status = "applied"  # type: ignore[union-attr]
            state.stage, state.final_status = Stage.DONE, FinalStatus.SUCCESS
        elif state.stage is Stage.REPORT:
            state.stage = Stage.DONE
        self.store.save(state)
        return state


class LocalService(RunService):
    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition not reached")


def test_review_endpoint_resumes_the_paused_run(tmp_path: Path) -> None:
    service = LocalService(Settings(), runs_root=tmp_path / "runs",
                           workflow_factory=lambda _settings, root: PausingWorkflow(root))  # type: ignore[arg-type,return-value]
    client = TestClient(create_app(service=service))
    run_id = service.start(RunCreateRequest(repo_url="https://github.com/org/app.git"))
    wait_for(lambda: service.store.load(run_id).stage is Stage.AWAIT_APPROVAL)

    assert client.post(f"/runs/{run_id}/review", json={"decision": "approved"}).status_code == 200

    wait_for(lambda: service.store.load(run_id).final_status is FinalStatus.SUCCESS)
    # A finished run can no longer be reviewed.
    assert client.post(f"/runs/{run_id}/review", json={"decision": "approved"}).status_code == 409
    assert client.post(f"/runs/{run_id}/review", json={"decision": "ship-it"}).status_code == 422


def test_paused_runs_survive_a_restart(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "runs")
    store.save(make_state(run_id="paused", stage=Stage.AWAIT_APPROVAL, workspace_path=str(tmp_path)))
    service = LocalService(Settings(), runs_root=tmp_path / "runs")

    assert service.recover_interrupted_runs() == []
    assert store.load("paused").final_status is None


def test_plan_approval_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFORGE_ENV_FILE", ".missing-test.env")
    monkeypatch.delenv("REQUIRE_PLAN_APPROVAL", raising=False)

    assert Settings.from_env().require_plan_approval is True
