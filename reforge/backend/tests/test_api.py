from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

from fastapi.testclient import TestClient

from reforge.backend.app.api.runs import DEFAULT_DISCOVERY_OBJECTIVE, RunCreateRequest, RunService
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.events import RunStateStore, TraceRecorder
from reforge.backend.app.core.state import AnalysisReport, FinalStatus, ProjectState, Stage, TestInventory as StateTestInventory
from reforge.backend.app.main import create_app
from reforge.backend.app.evaluation import BenchmarkCatalog, summarize_runs


RUNS_ROOT = Path(".api-test-runs")


class FakeWorkflow:
    def __init__(self, runs_root: Path) -> None:
        self.store = RunStateStore(runs_root)
        self.cancel_checker = None

    def initialize(self, *, run_id: str, repo_source: str, objective: str, workspace: Path) -> ProjectState:
        state = ProjectState(
            run_id=run_id,
            repo_source=repo_source,
            objective=objective,
            workspace_path=str(workspace),
            work_branch=f"reforge/{run_id}",
            base_commit="a" * 40,
        )
        TraceRecorder.add(state, agent="orchestrator", kind="start", summary="Fake workflow initialized.")
        self.store.save(state)
        return state

    def run(self, state: ProjectState) -> ProjectState:
        state.stage = Stage.REPORT
        state.final_status = FinalStatus.SUCCESS
        TraceRecorder.add(state, agent="orchestrator", kind="decision", summary="Fake workflow completed.")
        self.store.save(state)
        return state


class FakeRunService(RunService):
    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)


class FakeBenchmarkRunner:
    def __init__(self) -> None:
        self.catalog = BenchmarkCatalog(Path("benchmarks"))
        self.called: dict[str, object] | None = None

    def run_suite(self, **kwargs: object):
        self.called = kwargs
        return summarize_runs([])


def fake_service() -> RunService:
    return FakeRunService(
        Settings(),
        runs_root=RUNS_ROOT,
        workflow_factory=lambda _settings, root: FakeWorkflow(root),  # type: ignore[arg-type]
    )


def wait_for_terminal(service: RunService, run_id: str) -> ProjectState:
    deadline = monotonic() + 2
    while monotonic() < deadline:
        state = service.store.load(run_id)
        if state.final_status is not None:
            return state
        sleep(0.01)
    raise AssertionError("background run did not finish")


def test_post_run_is_background_and_exposes_persisted_state_and_sse() -> None:
    service = fake_service()
    client = TestClient(create_app(service=service))

    response = client.post(
        "/runs",
        json={"objective": "Modernize", "workspace_path": "controlled-fixture"},
    )

    assert response.status_code == 202
    run_id = response.json()["run_id"]
    state = wait_for_terminal(service, run_id)
    assert state.final_status is FinalStatus.SUCCESS
    assert client.get(f"/runs/{run_id}").json()["final_status"] == "SUCCESS"
    events = client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "-1"})
    assert events.status_code == 200
    assert "event: trace" in events.text
    assert "event: complete" in events.text
    assert client.get("/runs").status_code == 200


def test_run_uses_agent_led_discovery_when_no_objective_is_provided() -> None:
    service = fake_service()
    client = TestClient(create_app(service=service))

    response = client.post("/runs", json={"workspace_path": "controlled-fixture"})

    assert response.status_code == 202
    state = wait_for_terminal(service, response.json()["run_id"])
    assert state.objective == DEFAULT_DISCOVERY_OBJECTIVE


def test_resource_routes_return_state_slices_and_404_when_not_ready() -> None:
    service = fake_service()
    store = service.store
    state = ProjectState(
        run_id="api-state-slice",
        repo_source="fixture",
        objective="Modernize",
        workspace_path=str(Path.cwd()),
        work_branch="reforge/api-state-slice",
        base_commit="a" * 40,
        analysis=AnalysisReport(tests=StateTestInventory(), summary="ready"),
    )
    TraceRecorder.add(state, agent="analysis", kind="result", summary="Analysis is ready.")
    store.save(state)
    client = TestClient(create_app(service=service))

    assert client.get("/runs/api-state-slice/analysis").status_code == 200
    assert client.get("/runs/api-state-slice/plan").status_code == 404
    assert client.get("/runs/api-state-slice/tests").json() == {"baseline": None, "latest": None}
    assert client.get("/runs/missing").status_code == 404


def test_report_endpoint_serves_markdown_and_json_artifacts() -> None:
    service = fake_service()
    report_dir = RUNS_ROOT / "api-report"
    report_dir.mkdir(parents=True, exist_ok=True)
    markdown = report_dir / "report.md"
    markdown.write_text("# ReForge report\n", encoding="utf-8")
    markdown.with_suffix(".json").write_text('{"status":"SUCCESS"}', encoding="utf-8")
    state = ProjectState(
        run_id="api-report",
        repo_source="fixture",
        objective="Modernize",
        workspace_path=str(report_dir / "workspace"),
        work_branch="reforge/api-report",
        base_commit="a" * 40,
        stage=Stage.DONE,
        final_status=FinalStatus.SUCCESS,
        report_path=str(markdown),
    )
    service.store.save(state)
    client = TestClient(create_app(service=service))

    assert client.get("/runs/api-report/report").text == "# ReForge report\n"
    assert client.get("/runs/api-report/report?format=json").json() == {"status": "SUCCESS"}


def test_request_requires_exactly_one_repository_source() -> None:
    client = TestClient(create_app(service=fake_service()))

    assert client.post("/runs", json={"objective": "Modernize"}).status_code == 422
    assert client.post(
        "/runs", json={"objective": "Modernize", "repo_url": "x", "workspace_path": "y"}
    ).status_code == 422


def test_zip_upload_starts_a_background_run() -> None:
    service = fake_service()
    client = TestClient(create_app(service=service))

    response = client.post(
        "/runs",
        data={"objective": "Modernize uploaded project"},
        files={"upload": ("project.zip", b"not inspected by the fake materializer", "application/zip")},
    )

    assert response.status_code == 202
    assert wait_for_terminal(service, response.json()["run_id"]).final_status is FinalStatus.SUCCESS


def test_zip_upload_can_omit_an_objective() -> None:
    service = fake_service()
    client = TestClient(create_app(service=service))

    response = client.post(
        "/runs",
        files={"upload": ("project.zip", b"fake archive for fake materializer", "application/zip")},
    )

    assert response.status_code == 202
    assert wait_for_terminal(service, response.json()["run_id"]).objective == DEFAULT_DISCOVERY_OBJECTIVE


def test_cancel_marks_a_queued_run_aborted_and_benchmark_contract_is_exposed() -> None:
    service = fake_service()
    state = ProjectState(
        run_id="queued-cancel",
        repo_source="fixture",
        objective="Modernize",
        workspace_path="workspace",
        work_branch="reforge/queued-cancel",
        base_commit="",
    )
    service.store.save(state)
    client = TestClient(create_app(service=service))

    cancelled = client.post("/runs/queued-cancel/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["final_status"] == "ABORTED"
    assert client.get("/benchmarks").status_code == 200
    assert client.post("/benchmarks/run").status_code == 422


def test_benchmark_endpoint_uses_configured_evaluation_harness() -> None:
    evaluator = FakeBenchmarkRunner()
    client = TestClient(create_app(service=fake_service(), benchmark_runner=evaluator))  # type: ignore[arg-type]

    response = client.post(
        "/benchmarks/run",
        json={"names": ["flask-legacy-app"], "repetitions": 1, "ablation": "single_shot"},
    )

    assert response.status_code == 200
    assert evaluator.called is not None and evaluator.called["repetitions"] == 1
