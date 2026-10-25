"""Run management: delete stopped runs, rerun any run, and group runs by project."""

from __future__ import annotations

import io
import json
import threading
import zipfile
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from reforge.backend.app.api.runs import DEFAULT_DISCOVERY_OBJECTIVE, RunCreateRequest, RunService, _project_key
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.events import RunStateStore, TraceRecorder
from reforge.backend.app.core.state import FinalStatus, ProjectState, Stage
from reforge.backend.app.main import create_app


class RecordingWorkflow:
    """Finishes immediately (or blocks until released) and records what it was asked to run."""

    started: list[dict[str, str]] = []
    hold = threading.Event()

    def __init__(self, runs_root: Path) -> None:
        self.store = RunStateStore(runs_root)
        self.cancel_checker = None

    def initialize(self, *, run_id: str, repo_source: str, objective: str, workspace: Path) -> ProjectState:
        RecordingWorkflow.started.append({"run_id": run_id, "repo_source": repo_source, "objective": objective})
        state = ProjectState(
            run_id=run_id, repo_source=repo_source, objective=objective, workspace_path=str(workspace),
            work_branch=f"reforge/{run_id}", base_commit="a" * 40, stage=Stage.ANALYZE,
        )
        self.store.save(state)
        return state

    def run(self, state: ProjectState) -> ProjectState:
        while not RecordingWorkflow.hold.wait(0.01):
            pass
        state.stage = Stage.DONE
        state.final_status = FinalStatus.SUCCESS
        TraceRecorder.add(state, agent="orchestrator", kind="decision", summary="done")
        self.store.save(state)
        return state


class LocalService(RunService):
    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        if request.archive_path:
            super()._materialize(request, workspace)
            return
        workspace.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def reset_workflow():
    RecordingWorkflow.started = []
    RecordingWorkflow.hold.set()
    yield
    RecordingWorkflow.hold.set()


def make_service(tmp_path: Path) -> RunService:
    return LocalService(
        Settings(), runs_root=tmp_path / "runs",
        workflow_factory=lambda _settings, root: RecordingWorkflow(root),  # type: ignore[arg-type,return-value]
    )


def wait_terminal(service: RunService, run_id: str) -> ProjectState:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        state = service.store.load(run_id)
        if state.final_status is not None:
            return state
        sleep(0.01)
    raise AssertionError("run did not finish")


def zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_completed_run_can_be_deleted_with_all_artifacts(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    client = TestClient(create_app(service=service))
    run_id = service.start(RunCreateRequest(repo_url="https://github.com/org/app.git"))
    wait_terminal(service, run_id)

    response = client.delete(f"/runs/{run_id}")

    assert response.status_code == 200
    assert not (tmp_path / "runs" / run_id).exists()
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.delete(f"/runs/{run_id}").status_code == 404


def test_running_run_cannot_be_deleted(tmp_path: Path) -> None:
    RecordingWorkflow.hold.clear()
    service = make_service(tmp_path)
    client = TestClient(create_app(service=service))
    run_id = service.start(RunCreateRequest(repo_url="https://github.com/org/app.git"))

    response = client.delete(f"/runs/{run_id}")

    assert response.status_code == 409
    assert (tmp_path / "runs" / run_id / "state.json").is_file()


def test_rerun_repeats_a_git_run_as_a_new_run_in_the_same_project(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    client = TestClient(create_app(service=service))
    first = service.start(RunCreateRequest(repo_url="https://github.com/Agsyna/CharitEase.git", objective="Upgrade Express"))
    wait_terminal(service, first)

    response = client.post(f"/runs/{first}/rerun")

    assert response.status_code == 202
    second = response.json()["run_id"]
    assert second != first
    rerun = wait_terminal(service, second)
    original = service.store.load(first)
    assert rerun.repo_source == "https://github.com/Agsyna/CharitEase.git"
    assert rerun.objective == "Upgrade Express"
    assert rerun.project_key == original.project_key == "agsyna/charitease"


def test_rerun_of_a_discovery_run_starts_discovery_again(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    run_id = service.start(RunCreateRequest(repo_url="https://github.com/org/app.git"))
    finished = wait_terminal(service, run_id)
    # The workflow replaced the discovery request with an inferred objective.
    finished.objective = "Replace url.parse"
    finished.objective_origin = "inferred"
    service.store.save(finished)

    second = service.rerun(run_id)

    wait_terminal(service, second)
    assert RecordingWorkflow.started[-1]["objective"] == DEFAULT_DISCOVERY_OBJECTIVE


def test_rerun_of_a_zip_upload_reuses_the_saved_archive(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    client = TestClient(create_app(service=service))
    archive = zip_bytes({"charitease-main/package.json": json.dumps({"name": "charitease"}), "charitease-main/index.js": "1;\n"})
    created = client.post("/runs", files={"upload": ("CharitEase.zip", archive, "application/zip")})
    first = created.json()["run_id"]
    first_state = wait_terminal(service, first)

    second = client.post(f"/runs/{first}/rerun").json()["run_id"]

    second_state = wait_terminal(service, second)
    assert (tmp_path / "runs" / second / "upload.zip").read_bytes() == archive
    assert (tmp_path / "runs" / second / "workspace" / "package.json").is_file()  # extracted, single root stripped
    assert first_state.project_key == second_state.project_key == "charitease"
    assert second_state.project_name == "charitease"


def test_rerun_of_an_unknown_run_is_not_found(tmp_path: Path) -> None:
    client = TestClient(create_app(service=make_service(tmp_path)))

    assert client.post("/runs/doesnotexist/rerun").status_code == 404


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/agsyna/CharitEase.git",
        "https://github.com/Agsyna/charitease/",
        "git@github.com:agsyna/CharitEase.git",
        "ssh://git@github.com/agsyna/CharitEase",
    ],
)
def test_all_spellings_of_a_repository_share_one_project_key(url: str) -> None:
    assert _project_key(RunCreateRequest(repo_url=url)) == "agsyna/charitease"


def test_failed_setup_is_recorded_instead_of_hanging(tmp_path: Path) -> None:
    class FailingSetupService(RunService):
        def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
            raise RuntimeError("git clone failed: repository not found")

    service = FailingSetupService(
        Settings(), runs_root=tmp_path / "runs",
        workflow_factory=lambda _settings, root: RecordingWorkflow(root),  # type: ignore[arg-type,return-value]
    )
    run_id = service.start(RunCreateRequest(repo_url="https://github.com/org/missing.git"))

    state = wait_terminal(service, run_id)

    assert state.final_status is FinalStatus.FAILED
    assert "repository not found" in state.trace[-1].summary
