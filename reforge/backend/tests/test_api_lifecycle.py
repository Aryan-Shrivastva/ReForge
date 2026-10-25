"""API lifecycle fixes: validated overrides, cancellation without write races, restart recovery, upload limits."""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from reforge.backend.app.api.runs import CANCEL_MARKER, RunCreateRequest, RunService
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.events import RunStateStore, TraceRecorder
from reforge.backend.app.core.state import FinalStatus, ProjectState, Stage
from reforge.backend.app.main import create_app


class BlockingWorkflow:
    """A workflow that stays in-flight until released, honouring cancellation like the real one."""

    release = threading.Event()

    def __init__(self, runs_root: Path) -> None:
        self.store = RunStateStore(runs_root)
        self.cancel_checker = None

    def initialize(self, *, run_id: str, repo_source: str, objective: str, workspace: Path) -> ProjectState:
        state = ProjectState(
            run_id=run_id, repo_source=repo_source, objective=objective, workspace_path=str(workspace),
            work_branch=f"reforge/{run_id}", base_commit="a" * 40, stage=Stage.ANALYZE,
        )
        self.store.save(state)
        return state

    def run(self, state: ProjectState) -> ProjectState:
        while not self.release.wait(0.01):
            if self.cancel_checker and self.cancel_checker(state.run_id):
                state.stage = Stage.DONE
                state.final_status = FinalStatus.ABORTED
                TraceRecorder.add(state, agent="orchestrator", kind="decision", summary="Run cancelled by API request.")
                self.store.save(state)
                return state
            # The worker keeps saving progress, which used to overwrite an API-written ABORTED.
            TraceRecorder.add(state, agent="analysis", kind="tool_call", summary="working")
            self.store.save(state)
        state.stage = Stage.DONE
        state.final_status = FinalStatus.SUCCESS
        self.store.save(state)
        return state


class LocalService(RunService):
    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)


def service(tmp_path: Path, **settings: object) -> RunService:
    return LocalService(
        Settings(**settings),  # type: ignore[arg-type]
        runs_root=tmp_path / "runs",
        workflow_factory=lambda _settings, root: BlockingWorkflow(root),  # type: ignore[arg-type,return-value]
    )


def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition was not reached")


def test_cancelling_a_live_run_is_finalized_by_the_worker(tmp_path: Path) -> None:
    BlockingWorkflow.release.clear()
    runs = service(tmp_path)
    run_id = runs.start(RunCreateRequest(workspace_path="fixture"))
    wait_for(lambda: runs.store.load(run_id).stage is Stage.ANALYZE)

    view = runs.cancel(run_id)

    assert view.final_status is None  # the API did not write over the live worker
    assert "next checkpoint" in view.trace[-1].summary
    assert (tmp_path / "runs" / run_id / CANCEL_MARKER).is_file()
    wait_for(lambda: runs.store.load(run_id).final_status is FinalStatus.ABORTED)
    final = runs.store.load(run_id)
    assert final.trace[-1].summary == "Run cancelled by API request."


def test_interrupted_runs_are_recovered_on_startup(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "runs")
    store.save(ProjectState(run_id="stale", repo_source="x", objective="o", workspace_path="w",
                            work_branch="reforge/stale", base_commit="", stage=Stage.TEST))
    store.save(ProjectState(run_id="done", repo_source="x", objective="o", workspace_path="w",
                            work_branch="reforge/done", base_commit="", stage=Stage.DONE,
                            final_status=FinalStatus.SUCCESS))
    runs = service(tmp_path)

    # Constructing the service (or importing the app) must not touch runs.
    assert store.load("stale").final_status is None
    with TestClient(create_app(service=runs)):
        pass

    assert store.load("stale").final_status is FinalStatus.FAILED
    assert "restarted" in store.load("stale").trace[-1].summary
    assert store.load("done").final_status is FinalStatus.SUCCESS


def test_invalid_config_overrides_are_rejected_synchronously(tmp_path: Path) -> None:
    client = TestClient(create_app(service=service(tmp_path)))

    for overrides in ({"max_repairs": "lots"}, {"max_repairs": 0}, {"llm_provider": "skynet"}, {"unknown": 1}):
        response = client.post("/runs", json={"workspace_path": "fixture", "config_overrides": overrides})
        assert response.status_code == 422, overrides


def test_invalid_repository_url_is_rejected(tmp_path: Path) -> None:
    client = TestClient(create_app(service=service(tmp_path)))

    response = client.post("/runs", json={"repo_url": "ext::sh -c id"})

    assert response.status_code == 422


def test_oversized_upload_is_rejected(tmp_path: Path) -> None:
    client = TestClient(create_app(service=service(tmp_path, max_upload_bytes=1_024)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("big.txt", "x" * 50_000)

    response = client.post("/runs", files={"upload": ("repo.zip", buffer.getvalue(), "application/zip")})

    assert response.status_code == 413


def test_session_endpoint_exposes_agent_runtime_and_adk_state(tmp_path: Path) -> None:
    runs = service(tmp_path)
    runs.store.save(ProjectState(
        run_id="with-session", repo_source="x", objective="o", workspace_path="w", work_branch="reforge/with-session",
        base_commit="", agent_runtime={"analysis": "adk"}, agent_session={"stage": "PLAN", "agent_calls": 1},
    ))
    client = TestClient(create_app(service=runs))

    response = client.get("/runs/with-session/session")

    assert response.status_code == 200
    assert response.json() == {"agent_runtime": {"analysis": "adk"}, "state": {"stage": "PLAN", "agent_calls": 1}}


def test_concurrency_limit_queues_extra_runs(tmp_path: Path) -> None:
    BlockingWorkflow.release.clear()
    runs = service(tmp_path, max_concurrent_runs=1)
    first = runs.start(RunCreateRequest(workspace_path="fixture"))
    wait_for(lambda: runs.store.load(first).stage is Stage.ANALYZE)
    second = runs.start(RunCreateRequest(workspace_path="fixture"))

    wait_for(lambda: any("Waiting for a free run slot" in event.summary for event in runs.store.load(second).trace))
    assert runs.store.load(second).stage is Stage.INIT
    BlockingWorkflow.release.set()
    wait_for(lambda: runs.store.load(second).final_status is FinalStatus.SUCCESS)


@pytest.fixture(autouse=True)
def release_workers():
    yield
    BlockingWorkflow.release.set()
