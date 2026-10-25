"""Run lifecycle, persisted-state, and SSE API routes."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import threading
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.config import Settings
from ..core.events import RunStateStore, TraceRecorder
from ..core.state import FinalStatus, ProjectState, Stage
from ..core.workflow import DISCOVERY_OBJECTIVE, WorkflowRunner
from ..evaluation import BenchmarkCatalog, BenchmarkRunner, DEFAULT_ABLATIONS, EvaluationSummary
from ..tools.safe_git import initialize_repository, run_git

DEFAULT_DISCOVERY_OBJECTIVE = DISCOVERY_OBJECTIVE
CANCEL_MARKER = "cancel.requested"
_UPLOAD_CHUNK = 1024 * 1024
# Network transports only.  ``ext::`` runs an arbitrary command, ``file://``
# and bare paths read the backend's own filesystem, and a leading ``-`` would
# be parsed as a Git option.
_REPOSITORY_URL = re.compile(r"^(?:https?://|ssh://|git://)[^\s]+$|^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$")


class ArchiveError(ValueError):
    """Raised when an uploaded archive is unsafe or exceeds configured limits."""


class UploadTooLarge(ValueError):
    """Raised when an upload exceeds MAX_UPLOAD_BYTES."""


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
        if self.repo_url is not None and not _REPOSITORY_URL.match(self.repo_url.strip()):
            raise ValueError("repo_url must be an https://, http://, ssh://, git://, or user@host:path Git URL")
        return self


class RunCreated(BaseModel):
    run_id: str


class BenchmarkRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] | None = None
    repetitions: int = Field(default=3, ge=1)
    ablation: str = "iterative"


class RunService:
    """Own background run threads and persist state as the durable API contract.

    Concurrency rule: while a run's worker thread is alive, only that thread
    writes the run's ``state.json``.  The API communicates cancellation through
    an in-memory event plus an on-disk marker file, never by rewriting state
    underneath the worker (which would race with, and be overwritten by, the
    worker's next save).
    """

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
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_runs)

    def recover_interrupted_runs(self) -> list[str]:
        """Mark runs left non-terminal by a previous process as FAILED.

        Run workers are in-process threads; after a restart nothing will ever
        finish those runs, so leaving them "running" would hang clients forever.
        Called once from the application's startup lifespan, never on import.
        """
        recovered: list[str] = []
        for state in self.store.list():
            with self._lock:
                alive = state.run_id in self._threads and self._threads[state.run_id].is_alive()
            if state.final_status is not None or alive:
                continue
            cancelled = (self.runs_root / state.run_id / CANCEL_MARKER).is_file()
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED if cancelled else FinalStatus.FAILED
            TraceRecorder.add(
                state,
                agent="api",
                kind="error",
                summary="Run was interrupted because the ReForge service restarted before it finished.",
            )
            self.store.save(state)
            recovered.append(state.run_id)
        return recovered

    def start(self, request: RunCreateRequest) -> str:
        self.settings.with_overrides(request.config_overrides)
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
        if len(content) > self.settings.max_upload_bytes:
            raise UploadTooLarge(f"upload exceeds {self.settings.max_upload_bytes} bytes")
        self.settings.with_overrides(config_overrides)
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
        thread = threading.Thread(
            target=self._execute,
            args=(run_id, request),
            name=f"reforge-run-{run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._cancellations[run_id] = threading.Event()
            self._threads[run_id] = thread
        thread.start()
        return run_id

    def cancel(self, run_id: str) -> ProjectState:
        try:
            state = self.store.load(run_id)
        except FileNotFoundError as exc:
            raise KeyError(run_id) from exc
        marker = self.runs_root / run_id / CANCEL_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with self._lock:
            event = self._cancellations.setdefault(run_id, threading.Event())
            event.set()
            thread = self._threads.get(run_id)
        if state.final_status is not None:
            return state
        if thread is not None and thread.is_alive():
            # The worker observes the cancellation at its next checkpoint and
            # persists ABORTED itself.  Report the request without writing.
            view = state.model_copy(deep=True)
            TraceRecorder.add(
                view,
                agent="api",
                kind="decision",
                summary="Cancellation requested; the run stops at its next checkpoint.",
            )
            return view
        # No live worker (e.g. it finished setup failure handling or the
        # process restarted): finalize here.
        state.stage = Stage.REPORT
        state.final_status = FinalStatus.ABORTED
        TraceRecorder.add(state, agent="api", kind="decision", summary="Cancellation requested.")
        self.store.save(state)
        return state

    def cancelled(self, run_id: str) -> bool:
        with self._lock:
            event = self._cancellations.get(run_id)
        if event is not None and event.is_set():
            return True
        return (self.runs_root / run_id / CANCEL_MARKER).is_file()

    def _execute(self, run_id: str, request: RunCreateRequest) -> None:
        workspace = self.runs_root / run_id / "workspace"
        acquired = False
        try:
            if not self._slots.acquire(blocking=False):
                self._note(run_id, "Waiting for a free run slot (MAX_CONCURRENT_RUNS reached).")
                while not self._slots.acquire(timeout=1.0):
                    if self.cancelled(run_id):
                        self._finish_cancelled(run_id, request, workspace)
                        return
            acquired = True
            if self.cancelled(run_id):
                self._finish_cancelled(run_id, request, workspace)
                return
            self._materialize(request, workspace)
            if self.cancelled(run_id):
                self._finish_cancelled(run_id, request, workspace)
                return
            configured = self.settings.with_overrides(request.config_overrides)
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
        finally:
            if acquired:
                self._slots.release()

    def _note(self, run_id: str, summary: str) -> None:
        try:
            state = self.store.load(run_id)
        except (FileNotFoundError, ValueError):
            return
        TraceRecorder.add(state, agent="api", kind="decision", summary=summary)
        self.store.save(state)

    def _finish_cancelled(self, run_id: str, request: RunCreateRequest, workspace: Path) -> None:
        state = self._load_or_new(run_id, request, workspace)
        if state.final_status is None:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED
            TraceRecorder.add(state, agent="api", kind="decision", summary="Run cancelled before execution started.")
            self.store.save(state)

    def _materialize(self, request: RunCreateRequest, workspace: Path) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if request.archive_path:
            self._extract_archive(
                Path(request.archive_path),
                workspace,
                max_bytes=self.settings.max_archive_uncompressed_bytes,
                max_members=self.settings.max_archive_members,
            )
            initialize_repository(workspace, "Uploaded repository baseline")
            return
        source = request.workspace_path or request.repo_url
        assert source is not None
        completed = run_git(
            # Symlinks are checked out as plain files so a repository cannot
            # point workspace paths at the backend's filesystem.
            ["-c", "core.symlinks=false", "clone", "--no-recurse-submodules", "--", source, str(workspace)],
            timeout=300,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git clone failed")

    @staticmethod
    def _extract_archive(
        archive: Path,
        destination: Path,
        *,
        max_bytes: int = 1024 * 1024 * 1024,
        max_members: int = 50_000,
    ) -> None:
        """Extract a repository ZIP without its Git metadata, links, or unsafe paths.

        * ``.git`` directories are dropped: ``git init`` would otherwise keep a
          planted configuration (``core.fsmonitor``) or hooks, which host-side
          Git would execute.  The repository is re-initialized by ReForge.
        * Symlink entries are rejected; absolute, drive-letter, backslash, and
          ``..`` paths are rejected.
        * Declared uncompressed size and member count are bounded (zip bombs),
          and actual bytes written are counted while streaming.
        * A single top-level folder (as produced by GitHub "Download ZIP") is
          stripped so the project root is the workspace root.
        """
        try:
            destination.mkdir(parents=True, exist_ok=False)
            with zipfile.ZipFile(archive) as uploaded:
                members = uploaded.infolist()
                if len(members) > max_members:
                    raise ArchiveError(f"uploaded archive has more than {max_members} entries")
                if sum(member.file_size for member in members) > max_bytes:
                    raise ArchiveError(f"uploaded archive expands beyond {max_bytes} bytes")
                names: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
                for member in members:
                    raw = member.filename
                    if "\\" in raw or re.match(r"^[A-Za-z]:", raw):
                        raise ArchiveError("uploaded archive contains an unsafe path")
                    path = PurePosixPath(raw)
                    if path.is_absolute() or ".." in path.parts:
                        raise ArchiveError("uploaded archive contains an unsafe path")
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ArchiveError("uploaded archive contains a symbolic link")
                    if ".git" in path.parts or path.name == "":
                        continue
                    names.append((member, path))
                prefix = RunService._common_root(names)
                written = 0
                for member, path in names:
                    relative = PurePosixPath(*path.parts[len(prefix):]) if prefix else path
                    if not relative.parts:
                        continue
                    target = destination.joinpath(*relative.parts)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with uploaded.open(member) as source, target.open("wb") as sink:
                        while chunk := source.read(_UPLOAD_CHUNK):
                            written += len(chunk)
                            if written > max_bytes:
                                raise ArchiveError(f"uploaded archive expands beyond {max_bytes} bytes")
                            sink.write(chunk)
        except zipfile.BadZipFile as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise ArchiveError("uploaded file is not a valid ZIP archive") from exc
        except ArchiveError:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    @staticmethod
    def _common_root(names: list[tuple[zipfile.ZipInfo, PurePosixPath]]) -> tuple[str, ...]:
        """Return a single shared top-level directory, if every entry lives under it."""
        files = [path for member, path in names if not member.is_dir()]
        if not files:
            return ()
        first = files[0].parts[0]
        if all(len(path.parts) > 1 and path.parts[0] == first for path in files):
            return (first,)
        return ()

    def _load_or_new(self, run_id: str, request: RunCreateRequest, workspace: Path) -> ProjectState:
        try:
            return self.store.load(run_id)
        except FileNotFoundError:
            return ProjectState(
                run_id=run_id,
                repo_source=request.repo_url or request.workspace_path or request.archive_path or "",
                objective=request.objective,
                workspace_path=str(workspace),
                work_branch=f"reforge/{run_id}",
                base_commit="",
            )

    def _save_startup_failure(
        self,
        run_id: str,
        request: RunCreateRequest,
        workspace: Path,
        exc: Exception,
    ) -> None:
        state = self._load_or_new(run_id, request, workspace)
        if state.final_status is None:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED if self.cancelled(run_id) else FinalStatus.FAILED
            TraceRecorder.add(
                state,
                agent="api",
                kind="error",
                summary=f"Run setup failed: {type(exc).__name__}: {exc}",
            )
            self.store.save(state)


async def _read_upload(upload: Any, limit: int) -> bytes:
    """Read an upload in chunks, refusing to buffer more than ``limit`` bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadTooLarge(f"upload exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def build_router(service: RunService, *, benchmark_runner: BenchmarkRunner | None = None) -> APIRouter:
    router = APIRouter(tags=["runs"])

    @router.post("/runs", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: Request) -> RunCreated:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > service.settings.max_upload_bytes + _UPLOAD_CHUNK:
                raise HTTPException(status_code=413, detail="upload is too large")
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
                    content=await _read_upload(upload, service.settings.max_upload_bytes),
                    config_overrides=overrides,
                )
            except UploadTooLarge as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            except (ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return RunCreated(run_id=run_id)
        try:
            payload = RunCreateRequest.model_validate(await request.json())
            return RunCreated(run_id=service.start(payload))
        except RequestValidationError:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        except (KeyError, ValueError) as exc:
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

    @router.get("/runs/{run_id}/session")
    def get_session(run_id: str) -> dict[str, object]:
        """Agent runtime per role and the mirrored Google ADK session state."""
        state = _load(service, run_id)
        return {"agent_runtime": state.agent_runtime, "state": state.agent_session}

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
        if not state.base_commit or not Path(state.workspace_path).is_dir():
            raise HTTPException(status_code=404, detail="diff is not available")
        command = (
            ["show", "--no-ext-diff", "--no-textconv", "--format=", revision]
            if revision
            else ["diff", "--no-ext-diff", "--no-textconv", state.base_commit]
        )
        completed = run_git(command, cwd=state.workspace_path)
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
