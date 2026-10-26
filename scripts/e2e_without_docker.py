"""Run ReForge's real workflow end to end on bundled benchmarks, without Docker.

This is a development harness for machines without Docker.  Everything is the
production code path (deterministic orchestrator, real CrewAI/ADK agents
calling the configured LLM provider, real Git checkpoints, guards, reports)
except the sandbox: benchmark commands run in a local shell instead of a
disposable container.  For that reason it only accepts the trusted fixtures in
``benchmarks/`` - never point it at an untrusted repository.

It also measures provider efficiency: per-agent call counts, latency, token
usage, and how many agent attempts failed schema validation.

Usage (from the repository root, with .env configured):

    python scripts/e2e_without_docker.py node-legacy-app flask-legacy-app
    python scripts/e2e_without_docker.py --framework crewai node-legacy-app
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reforge.backend.app.agents.analysis import AnalysisAgent  # noqa: E402
from reforge.backend.app.agents.executor import AgentExecutors, build_agent_executors  # noqa: E402
from reforge.backend.app.agents.planning import PlanningAgent  # noqa: E402
from reforge.backend.app.agents.repair import RepairAgent  # noqa: E402
from reforge.backend.app.agents.test_generation import CharacterizationTestGenerator  # noqa: E402
from reforge.backend.app.agents.testing import TestRunner  # noqa: E402
from reforge.backend.app.agents.transformation import TransformationAgent  # noqa: E402
from reforge.backend.app.core.config import Settings  # noqa: E402
from reforge.backend.app.core.events import RunStateStore  # noqa: E402
from reforge.backend.app.core.workflow import WorkflowRunner, budgets_from_settings  # noqa: E402
from reforge.backend.app.evaluation import BenchmarkCatalog, BenchmarkRunner, evaluate_run  # noqa: E402
from reforge.backend.app.tools.ast_tools import RuleCatalog  # noqa: E402
from reforge.backend.app.tools.interfaces import ExecResult  # noqa: E402

WINDOWS = platform.system() == "Windows"
VENV_BIN = ".venv/Scripts" if WINDOWS else ".venv/bin"


class LocalShellSandbox:
    """Same contract as DockerSandbox, executed by a local POSIX shell (trusted fixtures only)."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.shell = shutil.which("sh") or shutil.which("bash")
        if self.shell is None:
            raise RuntimeError("a POSIX shell (sh/bash, e.g. Git Bash) is required")

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        command = cmd[-1] if cmd[:2] == ["/bin/sh", "-lc"] else " ".join(cmd)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [self.shell, "-c", command],
                cwd=self.workspace,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(124, _decode(exc.stdout), _decode(exc.stderr), time.monotonic() - started, True)
        return ExecResult(
            completed.returncode,
            _decode(completed.stdout),
            _decode(completed.stderr),
            time.monotonic() - started,
            False,
        )


def _decode(value: bytes | None) -> str:
    return (value or b"").decode("utf-8", errors="replace")


class LocalTestRunner(TestRunner):
    """The production runner with host-appropriate venv paths and test tooling installed in the venv."""

    __test__ = False

    @staticmethod
    def default_install_command(recreate_venv: bool = False) -> str:
        pip = f"{VENV_BIN}/python -m pip"
        commands = [
            "rm -rf .venv" if recreate_venv else "true",
            f"if [ ! -x {VENV_BIN}/python ] && [ ! -x {VENV_BIN}/python.exe ]; then python -m venv .venv; fi",
            # The Docker image ships these; a host venv must install them.
            f"{pip} install -q pytest pytest-json-report",
            f"if [ -f requirements.txt ]; then {pip} install -q -r requirements.txt; fi",
            f"if [ -f pyproject.toml ] || [ -f setup.py ] || [ -f setup.cfg ]; then {pip} install -q -e .; fi",
        ]
        return " && ".join(commands)

    @staticmethod
    def pytest_command() -> str:
        return (
            "mkdir -p .reforge-artifacts && "
            f"{VENV_BIN}/python -m pytest --json-report --json-report-file=.reforge-artifacts/pytest-report.json"
        )


@dataclass
class CallRecord:
    role: str
    framework: str
    output: str
    seconds: float
    ok: bool
    prompt_tokens: int
    completion_tokens: int
    error: str = ""


@dataclass
class TimedExecutor:
    """Wraps an executor to record latency, tokens, and validation failures per call."""

    inner: Any
    calls: list[CallRecord] = field(default_factory=list)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def run(self, *, role: str, output_model: type, **kwargs: Any) -> Any:
        before = (self.inner.usage.prompt_tokens, self.inner.usage.completion_tokens)
        started = time.monotonic()
        try:
            value = self.inner.run(role=role, output_model=output_model, **kwargs)
            ok, error = True, ""
            return value
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {exc}"[:400]
            raise
        finally:
            self.calls.append(
                CallRecord(
                    role=role,
                    framework=getattr(self.inner, "framework", "?"),
                    output=output_model.__name__,
                    seconds=time.monotonic() - started,
                    ok=ok,
                    prompt_tokens=self.inner.usage.prompt_tokens - before[0],
                    completion_tokens=self.inner.usage.completion_tokens - before[1],
                    error=error,
                )
            )


def timed_executors(settings: Settings) -> tuple[AgentExecutors, list[TimedExecutor]]:
    raw = build_agent_executors(settings)
    wrapped: dict[int, TimedExecutor] = {}
    for executor in raw.unique():
        wrapped[id(executor)] = TimedExecutor(executor)
    roles = {role: wrapped[id(raw.for_role(role))] for role in ("analysis", "planning", "transformation", "repair", "test_generation")}
    return AgentExecutors(**roles), list(wrapped.values())  # type: ignore[arg-type]


def build_runner(settings: Settings, runs_root: Path, executors: AgentExecutors) -> WorkflowRunner:
    catalog = RuleCatalog.from_yaml(ROOT / "reforge/backend/app/rules/python_deprecations.yaml")
    return WorkflowRunner(
        analysis_agent=AnalysisAgent(executor=executors.analysis),
        planning_agent=PlanningAgent(executor=executors.planning),
        transformation_agent=TransformationAgent(executor=executors.transformation),
        repair_agent=RepairAgent(executor=executors.repair),
        test_generator=CharacterizationTestGenerator(executor=executors.test_generation),
        testing_runner_factory=lambda workspace: LocalTestRunner(
            LocalShellSandbox(workspace), workspace, timeout_s=settings.sandbox_cmd_timeout_s
        ),
        catalog=catalog,
        state_store=RunStateStore(runs_root),
        # Codemods need the Docker image's pyupgrade/ruff/bump-pydantic.
        codemod_factory=None,
        enable_codemods=False,
        min_baseline_passing_tests=settings.min_baseline_passing_tests,
        allow_dependency_rollback_fallback=settings.allow_dependency_rollback_fallback,
        default_budgets=budgets_from_settings(settings),
        executors=executors,
    )


def run_benchmark(name: str, settings: Settings, runs_root: Path, discovery: bool) -> dict[str, Any]:
    catalog = BenchmarkCatalog(ROOT / "benchmarks")
    benchmark = catalog.get(name)
    run_id = f"e2e-{name}-{uuid.uuid4().hex[:6]}"
    workspace = runs_root / run_id / "workspace"
    BenchmarkRunner._materialize(benchmark.repository_path, workspace)

    executors, timed = timed_executors(settings)
    runner = build_runner(settings, runs_root, executors)
    state = runner.initialize(
        run_id=run_id,
        repo_source=str(benchmark.repository_path),
        objective="" if discovery else benchmark.objective,
        workspace=workspace,
    )
    print(f"\n=== {name} ({run_id}) | framework={settings.agent_framework} model={settings.llm_model} ===", flush=True)
    started = time.monotonic()
    state = runner.run(state)
    elapsed = time.monotonic() - started
    for executor in timed:
        close = getattr(executor.inner, "close", None)
        if callable(close):
            close()

    calls = [call for executor in timed for call in executor.calls]
    evaluation = evaluate_run(benchmark, state)
    stages = [event.data.get("to") for event in state.trace if event.data.get("to")]
    latest = state.test_runs[-1] if state.test_runs else None
    summary = {
        "benchmark": name,
        "run_id": run_id,
        "status": state.final_status.value if state.final_status else None,
        "objective": state.objective,
        "stage_path": stages,
        "agent_runtime": state.agent_runtime,
        "wall_clock_s": round(elapsed, 1),
        "llm_seconds": round(sum(call.seconds for call in calls), 1),
        "tokens_in": state.metrics.tokens_in,
        "tokens_out": state.metrics.tokens_out,
        "cost_usd": state.metrics.cost_usd,
        "trace_url": state.trace_url,
        "agent_calls": len(calls),
        "agent_call_failures": sum(not call.ok for call in calls),
        "baseline_passing": len(state.baseline_pass_set),
        "final_passing": len(latest.passed) if latest else 0,
        "behavior_preservation": evaluation.behavior_preservation,
        "detection_recall": evaluation.detection_recall,
        "regressions_introduced": evaluation.regressions_introduced,
        "plan": [f"{step.id} [{step.status}] {step.title}" for step in state.plan.steps] if state.plan else [],
        "repairs": [f"R{r.iteration}: {r.diagnosis} ({r.failing_before}->{r.failing_after})" for r in state.repairs],
        "replans": state.replans,
        "files_changed": state.metrics.files_changed,
        "report": state.report_path,
        "errors": [event.summary for event in state.trace if event.kind == "error"][-8:],
        "calls": [call.__dict__ for call in calls],
        "adk_session_keys": sorted(state.agent_session),
    }
    print_summary(summary)
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    print(f"status: {summary['status']}  | wall {summary['wall_clock_s']}s (LLM {summary['llm_seconds']}s)")
    print(f"objective: {summary['objective']}")
    print(f"stages: {' -> '.join(summary['stage_path'])}")
    print(f"tests: baseline {summary['baseline_passing']} -> final {summary['final_passing']} "
          f"| preservation {summary['behavior_preservation']:.0%} | regressions {summary['regressions_introduced']}")
    print(f"tokens: in {summary['tokens_in']:,} / out {summary['tokens_out']:,} | agent calls {summary['agent_calls']} "
          f"({summary['agent_call_failures']} failed) | est. cost ${summary['cost_usd']:.4f}")
    if summary.get("trace_url"):
        print(f"trace: {summary['trace_url']}")
    for line in summary["plan"]:
        print(f"  plan  {line}")
    for line in summary["repairs"]:
        print(f"  repair {line}")
    print("  per call:")
    for call in summary["calls"]:
        flag = "ok " if call["ok"] else "ERR"
        print(f"    {flag} {call['framework']:6} {call['output']:24} {call['seconds']:6.1f}s "
              f"in={call['prompt_tokens']:>6} out={call['completion_tokens']:>5} {call['error'][:90]}")
    for error in summary["errors"]:
        print(f"  trace error: {error[:200]}")
    print(f"report: {summary['report']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("benchmarks", nargs="+", help="names of fixtures in benchmarks/")
    parser.add_argument("--framework", choices=["crewai", "adk", "hybrid"], help="override AGENT_FRAMEWORK")
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--objective", action="store_true", help="use the benchmark objective instead of autonomous discovery")
    parser.add_argument("--runs-root", default=str(ROOT / "runs" / "e2e"))
    args = parser.parse_args()

    settings = Settings.from_env()
    overrides: dict[str, Any] = {}
    if args.framework:
        overrides["agent_framework"] = args.framework
    if args.model:
        overrides["llm_model"] = args.model
    settings = settings.with_overrides(overrides)
    runs_root = Path(args.runs_root).resolve()
    results = [run_benchmark(name, settings, runs_root, discovery=not args.objective) for name in args.benchmarks]
    output = runs_root / f"summary-{int(time.time())}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nSummary written to {output}")
    return 0 if all(result["status"] in {"SUCCESS", "PARTIAL"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
