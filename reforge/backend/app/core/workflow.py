"""M5 vertical workflow: deterministic routing around real agent and sandbox calls."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Protocol

from ..agents.analysis import AnalysisAgent, collect_analysis_evidence
from ..agents.planning import PlanningAgent, validate_plan
from ..agents.repair import RepairAgent
from ..agents.test_generation import CharacterizationTestGenerator
from ..agents.testing import TestRunner
from ..agents.transformation import TransformationAgent
from ..agents.crewai_adapter import build_crewai_executor
from ..core.config import Settings
from ..core.guards import MutationGuards
from ..core.state import (
    AnalysisReport,
    Budgets,
    FinalStatus,
    Plan,
    ProjectState,
    Stage,
    TestReport,
)
from ..reporting import ReportGenerator
from ..tools.ast_tools import RuleCatalog, scan_deprecated_patterns
from ..tools.codemod_tools import CodemodTools
from ..tools.git_tools import GitTools
from ..tools.repo_tools import RepositoryTools
from ..tools.sandbox import DockerSandbox
from .events import RunStateStore, TraceRecorder
from .orchestrator import Orchestrator, TransitionSignal


class TestingRunner(Protocol):
    def run(self, phase: str, **kwargs: object) -> TestReport: ...


class WorkflowError(RuntimeError):
    """Raised only for unrecoverable M5 workflow setup failures."""


class WorkflowRunner:
    """Own state, persist transitions, and invoke agents without LLM-controlled routing."""

    def __init__(
        self,
        *,
        analysis_agent: AnalysisAgent,
        planning_agent: PlanningAgent,
        transformation_agent: TransformationAgent,
        testing_runner_factory: Callable[[Path], TestingRunner],
        catalog: RuleCatalog,
        state_store: RunStateStore,
        codemod_factory: Callable[[Path], CodemodTools] | None = None,
        git_factory: Callable[[Path], GitTools] = GitTools,
        test_generator: CharacterizationTestGenerator | None = None,
        repair_agent: RepairAgent | None = None,
        min_baseline_passing_tests: int = 5,
        allow_dependency_rollback_fallback: bool = False,
        cancel_checker: Callable[[str], bool] | None = None,
        enable_codemods: bool = True,
        report_generator: ReportGenerator | None = None,
    ) -> None:
        self.analysis_agent = analysis_agent
        self.planning_agent = planning_agent
        self.transformation_agent = transformation_agent
        self.testing_runner_factory = testing_runner_factory
        self.catalog = catalog
        self.state_store = state_store
        self.codemod_factory = codemod_factory
        self.git_factory = git_factory
        self.test_generator = test_generator
        self.repair_agent = repair_agent
        if min_baseline_passing_tests < 1:
            raise ValueError("min_baseline_passing_tests must be positive")
        self.min_baseline_passing_tests = min_baseline_passing_tests
        self.allow_dependency_rollback_fallback = allow_dependency_rollback_fallback
        self.cancel_checker = cancel_checker
        self.enable_codemods = enable_codemods
        self.report_generator = report_generator or ReportGenerator(catalog)

    @classmethod
    def live(cls, settings: Settings, *, runs_root: Path = Path("runs")) -> "WorkflowRunner":
        """Build the standard real-CrewAI, Docker-sandboxed M5 workflow."""
        executor = build_crewai_executor(settings)
        catalog = RuleCatalog.from_yaml(
            Path(__file__).resolve().parents[1] / "rules" / "python_deprecations.yaml"
        )

        def sandbox_for(workspace: Path) -> DockerSandbox:
            return DockerSandbox(
                workspace,
                cpus=settings.sandbox_cpus,
                mem_limit=settings.sandbox_mem,
            )

        return cls(
            analysis_agent=AnalysisAgent(crew_executor=executor),
            planning_agent=PlanningAgent(crew_executor=executor),
            transformation_agent=TransformationAgent(crew_executor=executor),
            repair_agent=RepairAgent(crew_executor=executor),
            test_generator=CharacterizationTestGenerator(crew_executor=executor),
            testing_runner_factory=lambda workspace: TestRunner(
                sandbox_for(workspace), workspace, timeout_s=settings.sandbox_cmd_timeout_s
            ),
            catalog=catalog,
            state_store=RunStateStore(runs_root),
            codemod_factory=lambda workspace: CodemodTools(
                sandbox_for(workspace), workspace, timeout_s=settings.sandbox_cmd_timeout_s
            ),
            min_baseline_passing_tests=settings.min_baseline_passing_tests,
            allow_dependency_rollback_fallback=settings.allow_dependency_rollback_fallback,
        )

    def initialize(
        self,
        *,
        run_id: str,
        repo_source: str,
        objective: str,
        workspace: Path,
        budgets: Budgets | None = None,
    ) -> ProjectState:
        """Create the per-run branch and persisted INIT state for an existing Git workspace."""
        git = self.git_factory(workspace)
        state = ProjectState(
            run_id=run_id,
            repo_source=repo_source,
            objective=objective,
            workspace_path=str(workspace.resolve()),
            work_branch=f"reforge/{run_id}",
            base_commit=git.current_commit(),
            budgets=budgets or Budgets(),
        )
        state.best_commit = state.base_commit
        git.create_branch(state.work_branch)
        self._record(state, "orchestrator", "start", "Run initialized; working branch created.")
        return state

    def run(self, state: ProjectState) -> ProjectState:
        """Run the full M6 loop: baseline, transform, test, repair, and bounded replan."""
        started = monotonic()
        workspace = Path(state.workspace_path)
        if not workspace.is_dir():
            raise WorkflowError(f"workspace does not exist: {workspace}")
        if state.stage is Stage.INIT:
            self._transition(state, TransitionSignal.INITIALIZED, "Workspace is ready for analysis.")
        guards: MutationGuards | None = None

        try:
            while state.stage not in {Stage.REPORT, Stage.DONE}:
                self._check_budget(state, started)
                if state.stage is Stage.ANALYZE:
                    self._record(state, "analysis", "tool_call", "Collecting deterministic repository evidence.")
                    evidence = collect_analysis_evidence(workspace, self.catalog)
                    state.analysis = self._agent_call(
                        state, "analysis", lambda: self.analysis_agent.run(state.objective, evidence)
                    )
                    self._transition(state, TransitionSignal.ANALYSIS_VALID, "Analysis report validated.")
                elif state.stage is Stage.PLAN:
                    assert state.analysis is not None
                    state.plan = self._agent_call(
                        state,
                        "planning",
                        lambda: self.planning_agent.run(
                            state.objective, state.analysis, RepositoryTools(workspace).list_tree()
                        ),
                    )
                    validate_plan(state.plan)
                    self._transition(state, TransitionSignal.PLAN_VALID, "Modernization plan validated.")
                elif state.stage is Stage.BASELINE:
                    self._establish_baseline(state, workspace)
                elif state.stage is Stage.TRANSFORM:
                    assert state.plan is not None
                    if guards is None:
                        guards = self._make_guards(state)
                    self._apply_plan(state, workspace, guards)
                    self._transition(state, TransitionSignal.TRANSFORM_COMPLETE, "All independent plan steps attempted.")
                elif state.stage is Stage.TEST:
                    phase = "post_repair" if state.repairs and state.repairs[-1].failing_after is None else "post_transform"
                    report = self._run_tests(state, workspace, phase)
                    state.test_runs.append(report)
                    if state.repairs and state.repairs[-1].failing_after is None:
                        state.repairs[-1].failing_after = Orchestrator.failure_count(
                            report, state.baseline_pass_set
                        )
                        if state.repairs[-1].failing_after < state.repairs[-1].failing_before:
                            state.best_commit = self.git_factory(workspace).current_commit()
                    if self._meets_success_criteria(state, report, workspace):
                        self._transition(state, TransitionSignal.TEST_PASSED, "All M6 success criteria met.")
                    else:
                        self._route_test_failure(state)
                elif state.stage is Stage.REPAIR:
                    if self.repair_agent is None or guards is None or state.plan is None:
                        raise WorkflowError("repair stage requires a repair agent, plan, and initialized guards")
                    latest = state.test_runs[-1]
                    attempt = self._agent_call(
                        state,
                        "repair",
                        lambda: self.repair_agent.run(
                            objective=state.objective,
                            plan=state.plan,
                            report=latest,
                            repairs=state.repairs,
                            workspace=workspace,
                            guards=guards,
                            baseline_commit=state.best_commit or state.base_commit,
                            failing_before=Orchestrator.failure_count(latest, state.baseline_pass_set),
                        ),
                    )
                    state.repairs.append(attempt)
                    self._record(
                        state, "repair", "result", f"Repair {attempt.iteration} applied.",
                        {"files_changed": attempt.files_changed, "diagnosis": attempt.diagnosis},
                    )
                    self._transition(state, TransitionSignal.REPAIR_APPLIED, "Repair checkpoint committed.")
                elif state.stage is Stage.REPLAN:
                    self._replan(state, workspace)
                    guards = self._make_guards(state)
                    self._transition(state, TransitionSignal.REPLAN_VALID, "Replacement plan validated; restored best checkpoint.")
                else:  # pragma: no cover - all nonterminal states are handled above.
                    raise WorkflowError(f"unsupported workflow stage: {state.stage}")
        except Exception as exc:
            if state.final_status is not FinalStatus.ABORTED:
                self._report_failure(state, f"Workflow error: {type(exc).__name__}: {exc}")

        state.metrics.wall_clock_s += monotonic() - started
        if state.stage is Stage.REPORT:
            try:
                artifacts = self.report_generator.write(state)
                state.report_path = str(artifacts.markdown)
                self._transition(state, TransitionSignal.REPORT_WRITTEN, "Final Markdown and JSON reports written.")
            except Exception as exc:  # Preserve terminal run state even if its presentation artifact fails.
                self._record(state, "reporting", "error", f"Final report generation failed: {type(exc).__name__}: {exc}")
        self.state_store.save(state)
        return state

    def _agent_call(self, state: ProjectState, name: str, call: Callable[[], object]) -> object:
        self._record(state, name, "start", f"{name} agent started.")
        state.metrics.agent_calls[name] = state.metrics.agent_calls.get(name, 0) + 1
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                value = call()
                self._record(state, name, "result", f"{name} agent returned validated output.")
                return value
            except Exception as exc:
                last_error = exc
                self._record(state, name, "error", f"{name} agent attempt {attempt + 1} failed: {exc}")
        raise WorkflowError(f"{name} agent failed after one retry: {last_error}")

    def _establish_baseline(self, state: ProjectState, workspace: Path) -> None:
        """Freeze existing behavior, supplementing it only with passing generated tests."""
        report = self._run_tests(state, workspace, "baseline")
        state.test_runs.append(report)
        if report.install_ok and (
            state.analysis is None
            or not state.analysis.tests.has_tests
            or len(report.passed) < self.min_baseline_passing_tests
        ):
            if self.test_generator is None:
                raise WorkflowError("baseline needs characterization tests but no test generator is configured")
            for attempt in range(2):
                proposal = self._agent_call(
                    state,
                    "test_generation",
                    lambda: self.test_generator.propose(state.objective, workspace),
                )
                paths = self.test_generator.write(workspace, proposal)
                candidate = self._run_tests(state, workspace, "baseline")
                state.test_runs.append(candidate)
                generated_failures = [
                    failure
                    for failure in [*candidate.failed, *candidate.errored]
                    if (failure.file or "").startswith("tests/reforge_generated/")
                ]
                generated_passes = self._generated_test_passes(candidate)
                if candidate.install_ok and generated_passes and not generated_failures:
                    candidate.generated_tests = True
                    report = candidate
                    self._record(
                        state, "testing", "decision", "Generated characterization tests passed on original code.",
                        {"files": paths, "passing_tests": generated_passes, "attempt": attempt + 1},
                    )
                    break
                for path in paths:
                    RepositoryTools(workspace).resolve(path).unlink(missing_ok=True)
                self._record(
                    state, "testing", "decision", "Dropped generated tests that failed on original code.",
                    {"files": paths, "attempt": attempt + 1},
                )

        state.baseline = report
        state.baseline_pass_set = list(report.passed)
        state.protected_test_files = self._protected_test_files(state, workspace)
        if not report.install_ok or not state.baseline_pass_set:
            self._report_failure(state, "No usable baseline: install failed or no tests passed.")
            return
        generated_files = [path for path in state.protected_test_files if path.startswith("tests/reforge_generated/")]
        git = self.git_factory(workspace)
        if generated_files:
            git.commit("Baseline characterization tests", generated_files)
        state.best_commit = git.current_commit()
        self._transition(state, TransitionSignal.BASELINE_USABLE, "Baseline pass set frozen and checkpointed.")

    def _apply_plan(self, state: ProjectState, workspace: Path, guards: MutationGuards) -> None:
        assert state.plan is not None
        codemods = self.codemod_factory(workspace) if self.enable_codemods and self.codemod_factory else None
        for step in self._ordered_steps(state.plan):
            result = self._agent_call(
                state,
                "transformation",
                lambda step=step: self.transformation_agent.run(
                    objective=state.objective,
                    step=step,
                    workspace=workspace,
                    guards=guards,
                    codemods=codemods,
                ),
            )
            step.status = result.status
            step.commit_sha = result.commit_sha
            state.metrics.files_changed += len(result.files_changed)
            state.metrics.lines_added += sum(
                1 for line in result.diff.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
            state.metrics.lines_removed += sum(
                1 for line in result.diff.splitlines() if line.startswith("-") and not line.startswith("---")
            )
            self._record(
                state, "transformation", "result", f"{step.id} {result.status}.",
                {"files_changed": result.files_changed, "error": result.error},
            )

    def _route_test_failure(self, state: ProjectState) -> None:
        """Send an unsuccessful run to repair, replan, or its bounded terminal state."""
        self._transition(state, TransitionSignal.TEST_FAILED, "Post-change criteria were not met.")
        if state.stage is not Stage.REPAIR:
            return
        if self._consecutive_no_progress(state) >= state.budgets.max_no_progress:
            self._transition(state, TransitionSignal.REPAIR_NO_PROGRESS, "Repair progress stalled; replanning.")

    def _replan(self, state: ProjectState, workspace: Path) -> None:
        if state.analysis is None or state.plan is None or not state.test_runs:
            raise WorkflowError("replan requires analysis, a prior plan, and a test report")
        best_commit = state.best_commit or state.base_commit
        self.git_factory(workspace).reset_to(best_commit)
        previous = state.plan
        replacement = self._agent_call(
            state,
            "planning",
            lambda: self.planning_agent.replan(
                state.objective,
                state.analysis,
                RepositoryTools(workspace).list_tree(),
                previous,
                state.repairs,
                state.test_runs[-1],
            ),
        )
        validate_plan(replacement)
        for old_step in previous.steps:
            if old_step.status != "failed":
                continue
            for new_step in replacement.steps:
                if (
                    new_step.id == old_step.id
                    and new_step.title == old_step.title
                    and new_step.description == old_step.description
                    and new_step.files == old_step.files
                ):
                    raise WorkflowError("replan repeats a previously failed step in the same form")
        if replacement.version <= previous.version:
            replacement.version = previous.version + 1
        state.plan = replacement
        state.replans += 1

    def _make_guards(self, state: ProjectState) -> MutationGuards:
        dependencies = {
            finding.name: finding.current
            for finding in (state.analysis.dependencies if state.analysis else [])
            if finding.current
        }
        return MutationGuards(
            protected_test_files=set(state.protected_test_files),
            prior_dependency_constraints=dependencies,
            allow_dependency_rollback_fallback=self.allow_dependency_rollback_fallback,
        )

    @staticmethod
    def _consecutive_no_progress(state: ProjectState) -> int:
        count = 0
        for repair in reversed(state.repairs):
            if repair.failing_after is None or repair.failing_after < repair.failing_before:
                break
            count += 1
        return count

    @staticmethod
    def _generated_test_passes(report: TestReport) -> list[str]:
        """Return collected passing node IDs from the generated-test directory only."""
        return [
            test_id
            for test_id in report.passed
            if test_id.split("::", 1)[0].startswith("tests/reforge_generated/")
        ]

    def _run_tests(self, state: ProjectState, workspace: Path, phase: str) -> TestReport:
        self._record(state, "testing", "start", f"{phase} test run started.")
        runner = self.testing_runner_factory(workspace)
        report = runner.run(phase, recreate_venv=phase != "baseline")
        self._record(
            state,
            "testing",
            "result",
            f"{phase} test run completed.",
            {"passed": len(report.passed), "failed": len(report.failed), "errored": len(report.errored)},
        )
        return report

    def _transition(self, state: ProjectState, signal: TransitionSignal, summary: str) -> None:
        previous = state.stage
        Orchestrator.transition(state, signal)
        self._record(
            state,
            "orchestrator",
            "decision",
            summary,
            {"from": previous.value, "to": state.stage.value, "signal": signal.value},
        )

    def _record(
        self,
        state: ProjectState,
        agent: str,
        kind: str,
        summary: str,
        data: dict[str, object] | None = None,
    ) -> None:
        TraceRecorder.add(state, agent=agent, kind=kind, summary=summary, data=data)
        self.state_store.save(state)

    @staticmethod
    def _ordered_steps(plan: Plan) -> list:
        """Topologically order the already-validated plan deterministically."""
        by_id = {step.id: step for step in plan.steps}
        complete: set[str] = set()
        ordered = []
        while len(ordered) < len(plan.steps):
            available = [
                step
                for step in plan.steps
                if step.id not in complete and all(dependency in complete for dependency in step.depends_on)
            ]
            if not available:  # guarded by validate_plan, retained as a defensive check.
                raise WorkflowError("plan cannot be topologically ordered")
            for step in available:
                complete.add(step.id)
                ordered.append(by_id[step.id])
        return ordered

    @staticmethod
    def _protected_test_files(state: ProjectState, workspace: Path) -> list[str]:
        files = set(state.analysis.tests.test_files if state.analysis else [])
        for test_id in state.baseline_pass_set:
            if "::" in test_id:
                files.add(test_id.split("::", 1)[0])
        tests_root = workspace / "tests"
        if tests_root.is_dir():
            files.update(path.relative_to(workspace).as_posix() for path in tests_root.rglob("*.py"))
        return sorted(files)

    def _meets_success_criteria(self, state: ProjectState, report: TestReport, workspace: Path) -> bool:
        baseline = state.baseline
        if baseline is None or not report.install_ok:
            return False
        if not set(state.baseline_pass_set).issubset(report.passed):
            return False
        if report.errored:
            return False
        if baseline.lint_issues is not None and report.lint_issues is not None:
            if report.lint_issues > baseline.lint_issues:
                return False
        if baseline.typecheck_errors is not None and report.typecheck_errors is not None:
            if report.typecheck_errors > baseline.typecheck_errors:
                return False
        remaining = scan_deprecated_patterns(workspace.resolve(), self.catalog)
        return not remaining

    def _check_budget(self, state: ProjectState, started: float) -> None:
        if self.cancel_checker is not None and self.cancel_checker(state.run_id):
            Orchestrator.transition(state, TransitionSignal.CANCELLED)
            self._record(state, "orchestrator", "decision", "Run cancelled by API request.")
            raise WorkflowError("run cancelled")
        if monotonic() - started > state.budgets.max_wall_clock_s:
            Orchestrator.transition(state, TransitionSignal.BUDGET_EXCEEDED)
            self._record(state, "orchestrator", "decision", "Wall-clock budget exceeded.")
            raise WorkflowError("wall-clock budget exceeded")

    def _report_failure(self, state: ProjectState, summary: str) -> None:
        state.stage = Stage.REPORT
        state.final_status = FinalStatus.FAILED
        self._record(state, "orchestrator", "error", summary)
