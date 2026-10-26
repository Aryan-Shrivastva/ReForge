"""Controlled benchmark discovery and deterministic run-evaluation metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from statistics import mean
import shutil
import uuid
from collections.abc import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .core.state import Budgets, FinalStatus, ProjectState
from .core.workflow import WorkflowRunner
from .tools.safe_git import initialize_repository


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroundTruthPattern(StrictModel):
    rule_id: str
    file: str
    line: int = Field(ge=1)


class GroundTruth(StrictModel):
    deprecated_patterns: list[GroundTruthPattern] = Field(default_factory=list)
    target_dependencies: dict[str, str] = Field(default_factory=dict)


class BenchmarkDefinition(StrictModel):
    name: str
    objective: str
    ground_truth: GroundTruth
    success: str
    has_tests: bool
    repository_path: Path


class RunEvaluation(StrictModel):
    benchmark: str
    run_id: str
    status: FinalStatus | None
    behavior_preservation: float = Field(ge=0, le=1)
    repair_iterations: int = Field(ge=0)
    replans: int = Field(ge=0)
    detection_precision: float | None = Field(default=None, ge=0, le=1)
    detection_recall: float | None = Field(default=None, ge=0, le=1)
    files_changed: int = Field(ge=0)
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_clock_s: float = Field(ge=0)
    cost_usd: float = Field(default=0, ge=0)
    regressions_introduced: int = Field(ge=0)


class MeanSpread(StrictModel):
    mean: float
    standard_deviation: float = Field(ge=0)
    minimum: float
    maximum: float


class EvaluationSummary(StrictModel):
    runs: list[RunEvaluation]
    artifact_path: str | None = None
    success_rate: float = Field(ge=0, le=1)
    partial_rate: float = Field(ge=0, le=1)
    behavior_preservation: MeanSpread | None = None
    repair_iterations: MeanSpread | None = None
    replan_count: MeanSpread | None = None
    wall_clock_s: MeanSpread | None = None
    cost_usd: MeanSpread | None = None
    tokens_total: MeanSpread | None = None
    regressions_introduced: MeanSpread | None = None
    detection_precision: MeanSpread | None = None
    detection_recall: MeanSpread | None = None


class Ablation(StrictModel):
    name: str
    description: str
    max_repairs: int | None = Field(default=None, ge=0)
    max_replans: int | None = Field(default=None, ge=0)
    enable_codemods: bool | None = None


DEFAULT_ABLATIONS = (
    Ablation(name="iterative", description="Full ReForge workflow."),
    Ablation(name="single_shot", description="No repair attempts.", max_repairs=0),
    Ablation(name="no_codemods", description="Disable deterministic codemod execution.", enable_codemods=False),
    Ablation(name="no_replan", description="Disable the replan loop.", max_replans=0),
)


class BenchmarkCatalog:
    """Read repository-local benchmark manifests without executing benchmark code."""

    def __init__(self, root: Path = Path("benchmarks")) -> None:
        self.root = root.resolve()

    def list(self) -> list[BenchmarkDefinition]:
        if not self.root.is_dir():
            return []
        definitions: list[BenchmarkDefinition] = []
        for manifest in sorted(self.root.glob("*/benchmark.yaml")):
            payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"benchmark manifest must be a mapping: {manifest}")
            payload["repository_path"] = manifest.parent.resolve()
            definitions.append(BenchmarkDefinition.model_validate(payload))
        names = [definition.name for definition in definitions]
        if len(names) != len(set(names)):
            raise ValueError("benchmark names must be unique")
        return definitions

    def get(self, name: str) -> BenchmarkDefinition:
        for definition in self.list():
            if definition.name == name:
                return definition
        raise KeyError(name)


class BenchmarkRunner:
    """Run controlled repositories repeatedly and evaluate persisted workflow outcomes."""

    def __init__(
        self,
        catalog: BenchmarkCatalog,
        *,
        workflow_factory: Callable[[Path], WorkflowRunner],
        runs_root: Path = Path("benchmark-runs"),
    ) -> None:
        self.catalog = catalog
        self.workflow_factory = workflow_factory
        self.runs_root = runs_root.resolve()

    def run_suite(
        self,
        *,
        names: list[str] | None = None,
        repetitions: int = 3,
        ablation: Ablation = DEFAULT_ABLATIONS[0],
    ) -> EvaluationSummary:
        if repetitions < 1:
            raise ValueError("repetitions must be at least one")
        selected = [self.catalog.get(name) for name in names] if names else self.catalog.list()
        evaluations: list[RunEvaluation] = []
        for benchmark in selected:
            for iteration in range(repetitions):
                evaluations.append(self._run_once(benchmark, iteration, ablation))
        summary = summarize_runs(evaluations)
        summaries = self.runs_root / "summaries"
        summaries.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        artifact = summaries / f"evaluation-{ablation.name}-{timestamp}.json"
        summary.artifact_path = str(artifact)
        artifact.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        return summary

    def _run_once(self, benchmark: BenchmarkDefinition, iteration: int, ablation: Ablation) -> RunEvaluation:
        run_id = f"{benchmark.name}-{iteration + 1}-{uuid.uuid4().hex[:8]}"
        workspace = self.runs_root / run_id / "workspace"
        self._materialize(benchmark.repository_path, workspace)
        runner = self.workflow_factory(self.runs_root)
        # Benchmarks measure the unattended pipeline; there is no reviewer to wait for.
        runner.require_plan_approval = False
        if ablation.enable_codemods is not None:
            runner.enable_codemods = ablation.enable_codemods
        budgets = (getattr(runner, "default_budgets", None) or Budgets()).model_copy(
            update={
                key: value
                for key, value in {
                    "max_repairs": ablation.max_repairs,
                    "max_replans": ablation.max_replans,
                }.items()
                if value is not None
            }
        )
        state = runner.initialize(
            run_id=run_id,
            repo_source=str(benchmark.repository_path),
            objective=benchmark.objective,
            workspace=workspace,
            budgets=budgets,
        )
        return evaluate_run(benchmark, runner.run(state))

    @staticmethod
    def _materialize(source: Path, workspace: Path) -> None:
        workspace.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            workspace,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".reforge-artifacts"),
        )
        initialize_repository(workspace, "Benchmark baseline")


def evaluate_run(benchmark: BenchmarkDefinition, state: ProjectState) -> RunEvaluation:
    """Compute all Section 14 metrics directly from recorded state and ground truth."""
    latest = state.test_runs[-1] if state.test_runs else None
    baseline = set(state.baseline_pass_set)
    final_passed = set(latest.passed) if latest else set()
    behavior = len(baseline & final_passed) / len(baseline) if baseline else 0.0
    observed = {
        (pattern.rule_id, pattern.file, pattern.line)
        for pattern in (state.analysis.deprecated_patterns if state.analysis else [])
    }
    expected = {
        (pattern.rule_id, pattern.file, pattern.line)
        for pattern in benchmark.ground_truth.deprecated_patterns
    }
    precision = len(observed & expected) / len(observed) if observed else None
    recall = len(observed & expected) / len(expected) if expected else None
    failures = [*latest.failed, *latest.errored] if latest else []
    regressions = len(baseline - final_passed) + sum(
        failure.test_id not in baseline for failure in failures
    )
    return RunEvaluation(
        benchmark=benchmark.name,
        run_id=state.run_id,
        status=state.final_status,
        behavior_preservation=behavior,
        repair_iterations=len(state.repairs),
        replans=state.replans,
        detection_precision=precision,
        detection_recall=recall,
        files_changed=state.metrics.files_changed,
        lines_added=state.metrics.lines_added,
        lines_removed=state.metrics.lines_removed,
        tokens_in=state.metrics.tokens_in,
        tokens_out=state.metrics.tokens_out,
        wall_clock_s=state.metrics.wall_clock_s,
        cost_usd=state.metrics.cost_usd,
        regressions_introduced=regressions,
    )


def summarize_runs(runs: list[RunEvaluation]) -> EvaluationSummary:
    """Aggregate repeat runs, retaining means and population spread for nondeterministic LLM output."""
    total = len(runs)
    return EvaluationSummary(
        runs=runs,
        success_rate=sum(run.status is FinalStatus.SUCCESS for run in runs) / total if total else 0.0,
        partial_rate=sum(run.status is FinalStatus.PARTIAL for run in runs) / total if total else 0.0,
        behavior_preservation=_spread([run.behavior_preservation for run in runs]),
        repair_iterations=_spread([float(run.repair_iterations) for run in runs]),
        replan_count=_spread([float(run.replans) for run in runs]),
        wall_clock_s=_spread([run.wall_clock_s for run in runs]),
        cost_usd=_spread([run.cost_usd for run in runs]),
        tokens_total=_spread([float(run.tokens_in + run.tokens_out) for run in runs]),
        regressions_introduced=_spread([float(run.regressions_introduced) for run in runs]),
        detection_precision=_spread([run.detection_precision for run in runs if run.detection_precision is not None]),
        detection_recall=_spread([run.detection_recall for run in runs if run.detection_recall is not None]),
    )


def _spread(values: list[float]) -> MeanSpread | None:
    if not values:
        return None
    average = mean(values)
    return MeanSpread(
        mean=average,
        standard_deviation=sqrt(sum((value - average) ** 2 for value in values) / len(values)),
        minimum=min(values),
        maximum=max(values),
    )
