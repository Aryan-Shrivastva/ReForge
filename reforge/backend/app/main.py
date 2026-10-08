"""ASGI application for ReForge's dashboard and automation clients."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.runs import RunService, build_router
from .core.config import Settings
from .core.workflow import WorkflowRunner
from .evaluation import BenchmarkCatalog, BenchmarkRunner


def create_app(
    settings: Settings | None = None,
    *,
    service: RunService | None = None,
    benchmark_runner: BenchmarkRunner | None = None,
) -> FastAPI:
    """Create an app with dependency injection points for deterministic API tests."""
    configured = settings or Settings.from_env()
    run_service = service or RunService(configured)
    evaluator = benchmark_runner or BenchmarkRunner(
        BenchmarkCatalog(),
        workflow_factory=lambda root: WorkflowRunner.live(configured, runs_root=root),
        # Keep evaluation workspaces beneath /app/runs so Docker Desktop can
        # translate their bind mounts for the sibling execution sandbox.
        runs_root=run_service.runs_root / "evaluation",
    )
    app = FastAPI(title="ReForge API", version="0.1.0")
    # The Vite dashboard runs on its own local development origin.  Keeping this
    # explicit avoids opening the API to arbitrary browser origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.run_service = run_service
    app.include_router(build_router(run_service, benchmark_runner=evaluator))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
