"""Run lifecycle, persisted-state, and SSE API routes."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import fields, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.config import Settings
from ..core.events import RunStateStore, TraceRecorder
from ..core.state import FinalStatus, ProjectState, Stage
from ..core.workflow import WorkflowRunner
from ..evaluation import BenchmarkCatalog, BenchmarkRunner, DEFAULT_ABLATIONS, EvaluationSummary

DEFAULT_DISCOVERY_OBJECTIVE = (
    "Analyze this repository, identify safe modernization opportunities, and propose a prioritized plan."
)


class RunCreateRequest(BaseModel):
    """One repository source with optional human guidance and safe settings overrides."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(default=DEFAULT_DISCOVERY_OBJECTIVE)
    repo_url: str | None = None
    workspace_path: str | None = None
    archive_path: str | None = None
    config_overrides: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "RunCreateRequest":
        # A human may add a specific business goal, but discovery is the default:
        # the analysis agent is responsible for identifying upgrade candidates.
        self.objective = self.objective.strip() or DEFAULT_DISCOVERY_OBJECTIVE
        if sum(bool(source) for source in (self.repo_url, self.workspace_path, self.archive_path)) != 1:
            raise ValueError("provide exactly one repository URL, workspace path, or uploaded archive")
        return self


class RunCreated(BaseModel):
    run_id: str


class BenchmarkRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] | None = None
    repetitions: int = Field(default=3, ge=1)
    ablation: str = "iterative"


class RunService:
    """Own background run threads and persist state as the durable API contract."""

    def __init__(
        self,
        settings: Settings,
        *,
        runs_root: Path = Path("runs"),
        workflow_factory: Callable[[Settings, Path], WorkflowRunner] | None = None,
    ) -> None:
        self.settings = settings
        self.runs_root = runs_root.resolve()
        self.store = RunStateStore(self.runs_root)
        self.workflow_factory = workflow_factory or (
            lambda configured, root: WorkflowRunner.live(configured, runs_root=root)
        )
        self._cancellations: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, request: RunCreateRequest) -> str:
        run_id = uuid.uuid4().hex
        return self._start(run_id, request)

    def start_upload(
        self,
        *,
        objective: str | None,
        filename: str,
        content: bytes,
        config_overrides: dict[str, Any],
    ) -> str:
        """Persist a ZIP upload before the background worker safely extracts it."""
        if not filename.lower().endswith(".zip"):
            raise ValueError("uploaded repository must be a .zip archive")
        run_id = uuid.uuid4().hex
        archive = self.runs_root / run_id / "upload.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(content)
        return self._start(
            run_id,
            RunCreateRequest(
                objective=objective or DEFAULT_DISCOVERY_OBJECTIVE,
                archive_path=str(archive),
                config_overrides=config_overrides,
            ),
        )

    def _start(self, run_id: str, request: RunCreateRequest) -> str:
        workspace = self.runs_root / run_id / "workspace"
        state = ProjectState(
            run_id=run_id,
            repo_source=request.repo_url or request.workspace_path or request.archive_path or "",
            objective=request.objective,
            workspace_path=str(workspace),
            work_branch=f"reforge/{run_id}",
            base_commit="",
        )
        TraceRecorder.add(state, agent="api", kind="start", summary="Run queued for background execution.")
        self.store.save(state)
        with self._lock:
            self._cancellations[run_id] = threading.Event()
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, request),
            name=f"reforge-run-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return run_id

    def cancel(self, run_id: str) -> ProjectState:
        try:
            state = self.store.load(run_id)
        except FileNotFoundError as exc:
            raise KeyError(run_id) from exc
        with self._lock:
            event = self._cancellations.setdefault(run_id, threading.Event())
            event.set()
        if state.final_status is None:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED
            TraceRecorder.add(state, agent="api", kind="decision", summary="Cancellation requested.")
            self.store.save(state)
        return state

    def cancelled(self, run_id: str) -> bool:
        with self._lock:
            event = self._cancellations.get(run_id)
        return event.is_set() if event else False

    def _execute(self, run_id: str, request: RunCreateRequest) -> None:
        workspace = self.runs_root / run_id / "workspace"
        try:
            if self.cancelled(run_id):
                return
            self._materialize(request, workspace)
            if self.cancelled(run_id):
                return
            configured = self._settings_with_overrides(request.config_overrides)
            runner = self.workflow_factory(configured, self.runs_root)
            runner.cancel_checker = self.cancelled
            state = runner.initialize(
                run_id=run_id,
                repo_source=request.repo_url or request.workspace_path or request.archive_path or "",
                objective=request.objective,
                workspace=workspace,
            )
            runner.run(state)
        except Exception as exc:
            self._save_startup_failure(run_id, request, workspace, exc)

    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if request.archive_path:
            self._extract_archive(Path(request.archive_path), workspace)
            self._initialize_uploaded_repository(workspace)
            return
        source = request.workspace_path or request.repo_url
        assert source is not None
        completed = subprocess.run(
            ["git", "clone", "--", source, str(workspace)],
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git clone failed")

    @staticmethod
    def _extract_archive(archive: Path, destination: Path) -> None:
        try:
            destination.mkdir(parents=True, exist_ok=False)
            with zipfile.ZipFile(archive) as uploaded:
                for member in uploaded.infolist():
                    path = PurePosixPath(member.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise ValueError("uploaded archive contains an unsafe path")
                uploaded.extractall(destination)
        except zipfile.BadZipFile as exc:
            raise ValueError("uploaded file is not a valid ZIP archive") from exc

    @staticmethod
    def _initialize_uploaded_repository(workspace: Path) -> None:
        for command in (
            ["git", "init"],
            ["git", "config", "user.email", "reforge@local.invalid"],
            ["git", "config", "user.name", "ReForge"],
            ["git", "add", "."],
            ["git", "commit", "-m", "Uploaded repository baseline"],
        ):
            completed = subprocess.run(command, cwd=workspace, text=True, capture_output=True, check=False)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "could not initialize upload")

    def _settings_with_overrides(self, overrides: dict[str, Any]) -> Settings:
        permitted = {field.name for field in fields(Settings)}
        unknown = set(overrides) - permitted
        if unknown:
            raise ValueError(f"unsupported config overrides: {sorted(unknown)}")
        return replace(self.settings, **overrides)

    def _save_startup_failure(
        self,
        run_id: str,
        request: RunCreateRequest,
        workspace: Path,
        exc: Exception,
    ) -> None:
        try:
            state = self.store.load(run_id)
        except FileNotFoundError:
            state = ProjectState(
                run_id=run_id,
                repo_source=request.repo_url or request.workspace_path or request.archive_path or "",
                objective=request.objective,
                workspace_path=str(workspace),
                work_branch=f"reforge/{run_id}",
                base_commit="",
            )
        if state.final_status is None:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.FAILED
            TraceRecorder.add(
                state,
                agent="api",
                kind="error",
                summary=f"Run setup failed: {type(exc).__name__}: {exc}",
            )
            self.store.save(state)


def build_router(service: RunService, *, benchmark_runner: BenchmarkRunner | None = None) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.post("/runs", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: Request) -> RunCreated:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("upload")
            objective = form.get("objective")
            overrides_raw = form.get("config_overrides", "{}")
            if upload is None or not hasattr(upload, "read") or (objective is not None and not isinstance(objective, str)):
                raise HTTPException(status_code=422, detail="multipart requests require a ZIP upload")
            try:
                overrides = json.loads(str(overrides_raw))
                if not isinstance(overrides, dict):
                    raise ValueError("config_overrides must be a JSON object")
                run_id = service.start_upload(
                    objective=objective if isinstance(objective, str) else None,
                    filename=str(getattr(upload, "filename", "")),
                    content=await upload.read(),
                    config_overrides=overrides,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return RunCreated(run_id=run_id)
        try:
            payload = RunCreateRequest.model_validate(await request.json())
        except RequestValidationError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RunCreated(run_id=service.start(payload))

    @router.get("/runs", response_model=list[ProjectState])
    def list_runs() -> list[ProjectState]:
        return service.store.list()

    @router.get("/runs/{run_id}", response_model=ProjectState)
    def get_run(run_id: str) -> ProjectState:
        return _load(service, run_id)

    @router.post("/runs/{run_id}/cancel", response_model=ProjectState)
    def cancel_run(run_id: str) -> ProjectState:
        try:
            return service.cancel(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @router.get("/runs/{run_id}/analysis")
    def get_analysis(run_id: str):
        state = _load(service, run_id)
        if state.analysis is None:
            raise HTTPException(status_code=404, detail="analysis is not available")
        return state.analysis

    @router.get("/runs/{run_id}/plan")
    def get_plan(run_id: str):
        state = _load(service, run_id)
        if state.plan is None:
            raise HTTPException(status_code=404, detail="plan is not available")
        return state.plan

    @router.get("/runs/{run_id}/tests")
    def get_tests(run_id: str) -> dict[str, object | None]:
        state = _load(service, run_id)
        return {"baseline": state.baseline, "latest": state.test_runs[-1] if state.test_runs else None}

    @router.get("/runs/{run_id}/diff", response_class=PlainTextResponse)
    def get_diff(run_id: str, step: str | None = None) -> str:
        state = _load(service, run_id)
        revision: str | None = None
        if step is not None:
            if state.plan is None:
                raise HTTPException(status_code=404, detail="plan is not available")
            matching = next((item for item in state.plan.steps if item.id == step), None)
            if matching is None or matching.commit_sha is None:
                raise HTTPException(status_code=404, detail="step diff is not available")
            revision = matching.commit_sha
        command = ["git", "-C", state.workspace_path, "show", "--format=", revision] if revision else [
            "git", "-C", state.workspace_path, "diff", state.base_commit
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise HTTPException(status_code=404, detail="diff is not available")
        return completed.stdout

    @router.get("/runs/{run_id}/report", response_model=None)
    def get_report(run_id: str, format: Literal["markdown", "json"] = "markdown") -> PlainTextResponse | JSONResponse:
        state = _load(service, run_id)
        if state.report_path is None:
            raise HTTPException(status_code=404, detail="final report is not available")
        report = Path(state.report_path)
        if not report.is_file():
            raise HTTPException(status_code=404, detail="final report file is not available")
        if format == "json":
            json_report = report.with_suffix(".json")
            if not json_report.is_file():
                raise HTTPException(status_code=404, detail="final JSON report is not available")
            return JSONResponse(json.loads(json_report.read_text(encoding="utf-8")))
        return PlainTextResponse(report.read_text(encoding="utf-8"))

    @router.get("/runs/{run_id}/events")
    async def get_events(request: Request, run_id: str, last_event_id: int | None = None) -> StreamingResponse:
        _load(service, run_id)
        header = request.headers.get("last-event-id")
        try:
            cursor = last_event_id if last_event_id is not None else int(header) if header else -1
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
        return StreamingResponse(
            _event_stream(request, service, run_id, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/benchmarks")
    def list_benchmarks() -> list[object]:
        catalog = benchmark_runner.catalog if benchmark_runner else BenchmarkCatalog()
        return catalog.list()

    @router.post("/benchmarks/run", response_model=EvaluationSummary)
    async def run_benchmarks(payload: BenchmarkRunRequest) -> EvaluationSummary:
        if benchmark_runner is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="benchmark runner is unavailable")
        ablation = next((item for item in DEFAULT_ABLATIONS if item.name == payload.ablation), None)
        if ablation is None:
            raise HTTPException(status_code=422, detail=f"unknown ablation: {payload.ablation}")
        return await asyncio.to_thread(
            benchmark_runner.run_suite,
            names=payload.names,
            repetitions=payload.repetitions,
            ablation=ablation,
        )

    return router


def _load(service: RunService, run_id: str) -> ProjectState:
    try:
        return service.store.load(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


async def _event_stream(request: Request, service: RunService, run_id: str, cursor: int):
    """Replay durable trace events then poll until the terminal state is persisted."""
    while True:
        state = _load(service, run_id)
        for index, event in enumerate(state.trace):
            if index <= cursor:
                continue
            yield f"id: {index}\nevent: trace\ndata: {json.dumps(event.model_dump(mode='json'))}\n\n"
            cursor = index
        if state.final_status is not None:
            yield f"id: {cursor + 1}\nevent: complete\ndata: {json.dumps({'status': state.final_status.value})}\n\n"
            return
        if await request.is_disconnected():
            return
        yield ": keep-alive\n\n"
        await asyncio.sleep(0.2)
