"""Validated contracts shared by the orchestrator, agents, tools, and API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base schema: reject unknown fields so agent contracts cannot drift silently."""

    model_config = ConfigDict(extra="forbid")


class Stage(str, Enum):
    INIT = "INIT"
    ANALYZE = "ANALYZE"
    PLAN = "PLAN"
    BASELINE = "BASELINE"
    TRANSFORM = "TRANSFORM"
    TEST = "TEST"
    REPAIR = "REPAIR"
    REPLAN = "REPLAN"
    REPORT = "REPORT"
    DONE = "DONE"


class FinalStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class DependencyFinding(StrictModel):
    name: str
    current: str | None = None
    target: str | None = None
    status: Literal["ok", "outdated", "deprecated", "incompatible", "unpinned"]
    reason: str


class DeprecatedPattern(StrictModel):
    rule_id: str
    file: str
    line: int = Field(ge=1)
    snippet: str
    replacement_hint: str
    source: Literal["ast_rule", "runtime_warning", "llm"]


class TestInventory(StrictModel):
    framework: str | None = None
    test_files: list[str] = Field(default_factory=list)
    test_count: int = Field(default=0, ge=0)
    has_tests: bool = False


class AnalysisReport(StrictModel):
    languages: list[str] = Field(default_factory=list)
    python_version_current: str | None = None
    python_version_target: str | None = None
    dependencies: list[DependencyFinding] = Field(default_factory=list)
    deprecated_patterns: list[DeprecatedPattern] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    tests: TestInventory
    risks: list[str] = Field(default_factory=list)
    summary: str


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^S\d+$")
    title: str
    description: str
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    kind: Literal["dependency", "config", "api_replacement", "refactor", "other"]
    validation_hint: str
    status: Literal["pending", "applied", "failed", "skipped"] = "pending"
    commit_sha: str | None = None


class Plan(StrictModel):
    version: int = Field(ge=1)
    steps: list[PlanStep] = Field(min_length=1)
    rationale: str

    @field_validator("steps")
    @classmethod
    def unique_step_ids(cls, steps: list[PlanStep]) -> list[PlanStep]:
        ids = [step.id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        return steps


class PatchProposal(StrictModel):
    """One unified diff authored by the transformation reasoner."""

    unified_diff: str = Field(min_length=1)


class GeneratedTestFile(StrictModel):
    """One new baseline-only characterization test file."""

    path: str = Field(pattern=r"^tests/reforge_generated/[A-Za-z0-9_./-]+\.(?:py|test\.(?:js|mjs|cjs|ts))$")
    content: str = Field(min_length=1)


class TestGenerationProposal(StrictModel):
    """Structured output from the baseline test generator."""

    summary: str = Field(min_length=1)
    files: list[GeneratedTestFile] = Field(min_length=1)


class TransformationProposal(StrictModel):
    """Schema returned by the Transformation Agent before deterministic application."""

    summary: str = Field(min_length=1)
    patches: list[PatchProposal] = Field(default_factory=list)
    codemod: Literal["none", "pyupgrade", "ruff", "bump_pydantic"] = "none"


class TransformationResult(StrictModel):
    """Outcome of a single plan step; the orchestrator owns subsequent state updates."""

    step_id: str
    status: Literal["applied", "failed", "skipped"]
    files_changed: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    diff: str = ""
    error: str | None = None


class Failure(StrictModel):
    test_id: str
    error_type: str
    message: str
    traceback_excerpt: str
    file: str | None = None


class TestReport(StrictModel):
    phase: Literal["baseline", "post_transform", "post_repair"]
    install_ok: bool
    install_log_tail: str = ""
    passed: list[str] = Field(default_factory=list)
    failed: list[Failure] = Field(default_factory=list)
    errored: list[Failure] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    duration_s: float = Field(ge=0)
    lint_issues: int | None = Field(default=None, ge=0)
    typecheck_errors: int | None = Field(default=None, ge=0)
    generated_tests: bool = False


class RepairAttempt(StrictModel):
    iteration: int = Field(ge=1)
    diagnosis: str
    root_cause_category: Literal[
        "dependency", "api_change", "config", "logic", "test_env", "unknown"
    ]
    files_changed: list[str] = Field(default_factory=list)
    commit_sha: str | None = None
    failing_before: int = Field(ge=0)
    failing_after: int | None = Field(default=None, ge=0)


class RepairProposal(StrictModel):
    """A guarded, minimal fix proposed by the Repair Agent."""

    diagnosis: str = Field(min_length=1)
    root_cause_category: Literal[
        "dependency", "api_change", "config", "logic", "test_env", "unknown"
    ]
    patches: list[PatchProposal] = Field(min_length=1)


class Budgets(StrictModel):
    max_repairs: int = Field(default=5, ge=0)
    max_no_progress: int = Field(default=2, ge=1)
    max_replans: int = Field(default=1, ge=0)
    max_tokens: int = Field(default=500_000, ge=1)
    max_wall_clock_s: int = Field(default=1_800, ge=1)


class Metrics(StrictModel):
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    agent_calls: dict[str, int] = Field(default_factory=dict)
    wall_clock_s: float = Field(default=0, ge=0)
    files_changed: int = Field(default=0, ge=0)
    lines_added: int = Field(default=0, ge=0)
    lines_removed: int = Field(default=0, ge=0)


class TraceEvent(StrictModel):
    ts: datetime
    stage: Stage
    agent: str
    kind: Literal["start", "tool_call", "result", "decision", "error"]
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class ProjectState(StrictModel):
    run_id: str
    repo_source: str
    objective: str
    objective_origin: Literal["user", "discovery", "inferred"] = "user"
    workspace_path: str
    work_branch: str
    base_commit: str
    best_commit: str | None = None
    stage: Stage = Stage.INIT
    analysis: AnalysisReport | None = None
    plan: Plan | None = None
    baseline: TestReport | None = None
    baseline_pass_set: list[str] = Field(default_factory=list)
    protected_test_files: list[str] = Field(default_factory=list)
    # True when the repository had no runnable tests and the baseline is
    # per-file syntax checks only (behavior is then not verified).
    smoke_baseline: bool = False
    test_runs: list[TestReport] = Field(default_factory=list)
    repairs: list[RepairAttempt] = Field(default_factory=list)
    replans: int = Field(default=0, ge=0)
    budgets: Budgets = Field(default_factory=Budgets)
    metrics: Metrics = Field(default_factory=Metrics)
    trace: list[TraceEvent] = Field(default_factory=list)
    final_status: FinalStatus | None = None
    report_path: str | None = None
    # Agent role -> runtime ("crewai" or "adk") used for this run.
    agent_runtime: dict[str, str] = Field(default_factory=dict)
    # Mirror of the run's Google ADK session state (empty when no ADK agent runs).
    agent_session: dict[str, Any] = Field(default_factory=dict)
