"""M5 vertical workflow: deterministic routing around real agent and sandbox calls."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from ..agents.analysis import AnalysisAgent, collect_analysis_evidence
from ..agents.executor import AgentExecutors, TokenUsage, build_agent_executors
from ..agents.planning import PlanningAgent, validate_plan
from ..agents.repair import RepairAgent
from ..agents.test_generation import CharacterizationTestGenerator
from ..agents.testing import TestRunner
from ..agents.transformation import TransformationAgent
from ..core.config import Settings
from ..core.guards import MutationGuards
from ..core.state import (
    AnalysisReport,
    Budgets,
    DeprecatedPattern,
    FinalStatus,
    Plan,
    ProjectState,
    Stage,
    TestReport,
)
from ..reporting import ReportGenerator
from ..tools.ast_tools import RuleCatalog, scan_all_deprecated_patterns
from ..tools.codemod_tools import CodemodTools
from ..tools.git_tools import GitTools
from ..tools.languages import is_test_file, iter_source_files
from ..tools.repo_tools import RepositoryTools
from ..tools.sandbox import DockerSandbox
from .events import RunStateStore, TraceRecorder
from .orchestrator import Orchestrator, TransitionSignal


DISCOVERY_OBJECTIVE = (
    "Analyze this repository, identify safe modernization opportunities, and propose a prioritized plan."
)
MAX_INFERRED_OBJECTIVE_LENGTH = 320
GENERATED_TEST_PREFIX = "tests/reforge_generated/"
_TEST_FILE_EXTENSIONS = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".java", ".kt")


class TestingRunner(Protocol):
    def run(self, phase: str, **kwargs: object) -> TestReport: ...


class WorkflowError(RuntimeError):
    """Raised only for unrecoverable M5 workflow setup failures."""


def budgets_from_settings(settings: Settings) -> Budgets:
    """Translate environment/API settings into the per-run budgets the orchestrator enforces."""
    return Budgets(
        max_repairs=settings.max_repairs,
        max_no_progress=settings.max_no_progress,
        max_replans=settings.max_replans,
        max_tokens=settings.max_tokens,
        max_wall_clock_s=settings.max_wall_clock_s,
    )


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
        default_budgets: Budgets | None = None,
        executors: AgentExecutors | None = None,
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
        self.default_budgets = default_budgets or Budgets()
        self.executors = executors

    @classmethod
    def live(cls, settings: Settings, *, runs_root: Path = Path("runs")) -> "WorkflowRunner":
        """Build the standard Docker-sandboxed workflow on the configured agent runtime(s)."""
        executors = build_agent_executors(settings)
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
            analysis_agent=AnalysisAgent(executor=executors.analysis),
            planning_agent=PlanningAgent(executor=executors.planning),
            transformation_agent=TransformationAgent(executor=executors.transformation),
            repair_agent=RepairAgent(executor=executors.repair),
            test_generator=CharacterizationTestGenerator(executor=executors.test_generation),
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
            default_budgets=budgets_from_settings(settings),
            executors=executors,
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
        submitted_objective = objective.strip()
        discovery_requested = not submitted_objective or submitted_objective == DISCOVERY_OBJECTIVE
        state = ProjectState(
            run_id=run_id,
            repo_source=repo_source,
            objective=DISCOVERY_OBJECTIVE if discovery_requested else submitted_objective,
            objective_origin="discovery" if discovery_requested else "user",
            workspace_path=str(workspace.resolve()),
            work_branch=f"reforge/{run_id}",
            base_commit=git.current_commit(),
            budgets=budgets or self.default_budgets.model_copy(),
        )
        state.best_commit = state.base_commit
        git.create_branch(state.work_branch)
        self._bind_agent_sessions(state)
        self._record(state, "orchestrator", "start", "Run initialized; working branch created.")
        return state

    def run(self, state: ProjectState) -> ProjectState:
        """Run the full M6 loop: baseline, transform, test, repair, and bounded replan."""
        started = monotonic()
        workspace = Path(state.workspace_path)
        if not workspace.is_dir():
            raise WorkflowError(f"workspace does not exist: {workspace}")
        if not state.agent_runtime and self.executors is not None:
            self._bind_agent_sessions(state)
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
                    if state.objective_origin == "discovery":
                        state.objective = self._infer_objective(state.analysis)
                        state.objective_origin = "inferred"
                        self._record(
                            state,
                            "orchestrator",
                            "decision",
                            "Inferred a bounded modernization objective from deterministic analysis findings.",
                            {"objective": state.objective},
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
                    if not any(step.status == "applied" for step in state.plan.steps):
                        self._report_partial(
                            state,
                            "No planned modernization step could be applied; baseline behavior was preserved but no code change was produced.",
                        )
                    else:
                        self._transition(state, TransitionSignal.TRANSFORM_COMPLETE, "All independent plan steps attempted.")
                elif state.stage is Stage.TEST:
                    self._evaluate_tests(state, workspace)
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
        self._sync_agent_sessions(state)
        self.state_store.save(state)
        return state

    def _evaluate_tests(self, state: ProjectState, workspace: Path) -> None:
        """Run the post-change suite and route on what it actually shows.

        Regressions are checked before plan completeness: a run whose applied
        steps broke frozen baseline tests must go to repair, never be reported
        as a PARTIAL "baseline preserved" result.
        """
        post_repair = bool(state.repairs and state.repairs[-1].failing_after is None)
        report = self._run_tests(state, workspace, "post_repair" if post_repair else "post_transform")
        state.test_runs.append(report)
        if post_repair:
            state.repairs[-1].failing_after = Orchestrator.failure_count(report, state.baseline_pass_set)
            if state.repairs[-1].failing_after < state.repairs[-1].failing_before:
                state.best_commit = self.git_factory(workspace).current_commit()

        regressions = self._regressions(state, report)
        if self._meets_success_criteria(state, report, workspace):
            self._transition(state, TransitionSignal.TEST_PASSED, "All M6 success criteria met.")
        elif regressions:
            self._record(
                state, "testing", "decision", "Post-change run regressed the frozen baseline.",
                {"regressions": regressions[:50], "count": len(regressions)},
            )
            self._route_test_failure(state)
        elif self._has_unapplied_plan_steps(state):
            unapplied = [step.id for step in state.plan.steps if step.status != "applied"] if state.plan else []
            self._report_partial(
                state,
                "Applied steps preserved every frozen baseline test, but some planned modernization steps were not "
                "applied; the run is incomplete.",
                {"unapplied_steps": unapplied},
            )
        else:
            self._route_test_failure(state)

    def _agent_call(self, state: ProjectState, name: str, call: Callable[[], object]) -> object:
        self._record(state, name, "start", f"{name} agent started.", {"framework": state.agent_runtime.get(name)})
        state.metrics.agent_calls[name] = state.metrics.agent_calls.get(name, 0) + 1
        last_error: Exception | None = None
        try:
            for attempt in range(2):
                before = self._executor_usage()
                try:
                    value = call()
                    self._record(state, name, "result", f"{name} agent returned validated output.")
                    return value
                except Exception as exc:
                    last_error = exc
                    self._record(state, name, "error", f"{name} agent attempt {attempt + 1} failed: {exc}")
                finally:
                    self._account_tokens(state, before)
            raise WorkflowError(f"{name} agent failed after one retry: {last_error}")
        finally:
            self._sync_agent_sessions(state)
            self._enforce_token_budget(state)

    def _executor_usage(self) -> TokenUsage:
        return self.executors.usage() if self.executors is not None else TokenUsage()

    def _account_tokens(self, state: ProjectState, before: TokenUsage) -> None:
        after = self._executor_usage()
        state.metrics.tokens_in += max(after.prompt_tokens - before.prompt_tokens, 0)
        state.metrics.tokens_out += max(after.completion_tokens - before.completion_tokens, 0)

    def _enforce_token_budget(self, state: ProjectState) -> None:
        used = state.metrics.tokens_in + state.metrics.tokens_out
        if used > state.budgets.max_tokens and state.final_status is None:
            Orchestrator.transition(state, TransitionSignal.BUDGET_EXCEEDED)
            self._record(
                state, "orchestrator", "decision", "Token budget exceeded.",
                {"tokens_used": used, "max_tokens": state.budgets.max_tokens},
            )
            raise WorkflowError("token budget exceeded")

    def _bind_agent_sessions(self, state: ProjectState) -> None:
        """Record which runtime runs each agent and open ADK sessions with initial run state."""
        if self.executors is None:
            return
        state.agent_runtime = self.executors.frameworks()
        for executor in self.executors.session_executors():
            executor.bind_run(state.run_id, self._session_snapshot(state))
        self._sync_agent_sessions(state, push=False)

    def _sync_agent_sessions(self, state: ProjectState, *, push: bool = True) -> None:
        """Push orchestrator facts into ADK session state and mirror it back into the run record."""
        if self.executors is None:
            return
        for executor in self.executors.session_executors():
            try:
                if push:
                    executor.update_run_state(self._session_snapshot(state))
                state.agent_session = executor.run_state()
            except Exception as exc:  # Session memory is advisory; never fail a run over it.
                TraceRecorder.add(
                    state, agent="orchestrator", kind="error",
                    summary=f"ADK session state sync failed: {type(exc).__name__}: {exc}",
                )

    @staticmethod
    def _session_snapshot(state: ProjectState) -> dict[str, Any]:
        """Compact, JSON-safe run memory written into ADK session state."""
        analysis = state.analysis
        plan = state.plan
        return {
            "run_id": state.run_id,
            "repository": state.repo_source,
            "objective": state.objective,
            "objective_origin": state.objective_origin,
            "stage": state.stage.value,
            "final_status": state.final_status.value if state.final_status else "",
            "languages": ", ".join(analysis.languages) if analysis else "",
            "analysis_summary": (
                f"{analysis.summary} ({len(analysis.dependencies)} dependencies, "
                f"{len(analysis.deprecated_patterns)} deprecated patterns, "
                f"{analysis.tests.test_count} tests via {analysis.tests.framework or 'unknown'})"
                if analysis else ""
            ),
            "plan_summary": (
                f"v{plan.version}: " + "; ".join(f"{step.id} [{step.status}] {step.title}" for step in plan.steps)
                if plan else ""
            ),
            "repair_history": "; ".join(
                f"R{repair.iteration} {repair.root_cause_category}: {repair.diagnosis[:160]} "
                f"(failing {repair.failing_before} -> {repair.failing_after if repair.failing_after is not None else '?'})"
                for repair in state.repairs
            ),
            "replans": state.replans,
            "baseline_passing": len(state.baseline_pass_set),
            "tokens_used": state.metrics.tokens_in + state.metrics.tokens_out,
        }

    def _establish_baseline(self, state: ProjectState, workspace: Path) -> None:
        """Freeze existing behavior, supplementing it only with passing generated tests."""
        report = self._run_tests(state, workspace, "baseline")
        state.test_runs.append(report)
        if report.install_ok and (
            state.analysis is None
            or not state.analysis.tests.has_tests
            or len(report.passed) < self.min_baseline_passing_tests
        ) and self.test_generator is not None and self.test_generator.supports(workspace):
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
                    if (failure.file or failure.test_id).startswith(GENERATED_TEST_PREFIX)
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
        generated_files = [path for path in state.protected_test_files if path.startswith(GENERATED_TEST_PREFIX)]
        git = self.git_factory(workspace)
        if generated_files:
            git.commit("Baseline characterization tests", generated_files)
        state.best_commit = git.current_commit()
        self._transition(state, TransitionSignal.BASELINE_USABLE, "Baseline pass set frozen and checkpointed.")

    def _apply_plan(self, state: ProjectState, workspace: Path, guards: MutationGuards) -> None:
        assert state.plan is not None
        codemods = self.codemod_factory(workspace) if self.enable_codemods and self.codemod_factory else None
        for step in self._ordered_steps(state.plan):
            blocked_by = [
                dependency
                for dependency in step.depends_on
                if next(item for item in state.plan.steps if item.id == dependency).status != "applied"
            ]
            if blocked_by:
                step.status = "skipped"
                self._record(
                    state,
                    "transformation",
                    "decision",
                    f"{step.id} skipped because prerequisite step(s) did not apply.",
                    {"blocked_by": blocked_by},
                )
                continue
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
    def _infer_objective(analysis: AnalysisReport) -> str:
        """Produce a small, auditable objective without asking an LLM to route work.

        Discovery requests intentionally begin broad. Once the analyst has
        validated evidence, planning receives this deterministic description of
        the highest-confidence work instead of an empty or unbounded request.
        """
        upgrade_targets = [
            finding
            for finding in analysis.dependencies
            if finding.status in {"outdated", "deprecated", "incompatible"} and finding.target
        ]
        deprecated_packages = [
            finding.name for finding in analysis.dependencies if finding.status == "deprecated" and not finding.target
        ]
        if upgrade_targets:
            rendered = ", ".join(
                f"{item.name} to {item.target}" for item in upgrade_targets[:3]
            )
            objective = f"Upgrade and validate {rendered}; resolve the associated compatibility findings."
        elif analysis.deprecated_patterns:
            rules = ", ".join(sorted({item.rule_id for item in analysis.deprecated_patterns})[:3])
            objective = f"Replace the detected deprecated API patterns ({rules}) and validate preserved behavior."
        elif deprecated_packages:
            objective = (
                f"Replace the deprecated package(s) {', '.join(sorted(deprecated_packages)[:3])} with maintained "
                "alternatives and validate preserved behavior."
            )
        elif analysis.python_version_target:
            objective = (
                f"Modernize compatibility for Python {analysis.python_version_target} and validate preserved behavior."
            )
        else:
            objective = (
                "Modernize the repository's highest-confidence compatibility and maintenance findings "
                "while preserving the baseline test behavior."
            )
        return objective[:MAX_INFERRED_OBJECTIVE_LENGTH].rstrip()

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
        """Return collected passing test IDs from the generated-test directory only."""
        return [
            test_id
            for test_id in report.passed
            if test_id.split("::", 1)[0].startswith(GENERATED_TEST_PREFIX)
        ]

    def _run_tests(self, state: ProjectState, workspace: Path, phase: str) -> TestReport:
        self._record(state, "testing", "start", f"{phase} test run started.")
        runner = self.testing_runner_factory(workspace)
        dependency_changed = bool(
            phase != "baseline"
            and state.plan
            and any(step.status == "applied" and step.kind == "dependency" for step in state.plan.steps)
        )
        report = runner.run(phase, recreate_venv=dependency_changed)
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
        self._sync_agent_sessions(state)

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
        """Freeze every test file in any supported language, plus files of passing baseline tests."""
        files = set(state.analysis.tests.test_files if state.analysis else [])
        for test_id in state.baseline_pass_set:
            if "::" in test_id:
                files.add(test_id.split("::", 1)[0])
        root = workspace.resolve()
        for path in iter_source_files(root, _TEST_FILE_EXTENSIONS):
            relative = path.relative_to(root).as_posix()
            if is_test_file(relative) or relative.startswith("tests/"):
                files.add(relative)
        return sorted(files)

    @staticmethod
    def _regressions(state: ProjectState, report: TestReport) -> list[str]:
        """Frozen baseline tests that no longer pass, new errors, or a failed install."""
        if not report.install_ok:
            return ["<install>"]
        passed = set(report.passed)
        missing = [test_id for test_id in state.baseline_pass_set if test_id not in passed]
        baseline_errors = {failure.test_id for failure in (state.baseline.errored if state.baseline else [])}
        new_errors = [failure.test_id for failure in report.errored if failure.test_id not in baseline_errors]
        return [*missing, *[test_id for test_id in new_errors if test_id not in missing]]

    def _meets_success_criteria(self, state: ProjectState, report: TestReport, workspace: Path) -> bool:
        """Specification Section 7: all five success criteria must hold."""
        baseline = state.baseline
        if baseline is None or not report.install_ok:
            return False
        if self._has_unapplied_plan_steps(state):
            return False
        if self._regressions(state, report):
            return False
        if baseline.lint_issues is not None and report.lint_issues is not None:
            if report.lint_issues > baseline.lint_issues:
                return False
        if baseline.typecheck_errors is not None and report.typecheck_errors is not None:
            if report.typecheck_errors > baseline.typecheck_errors:
                return False
        return not self.remaining_targeted_patterns(state, workspace)

    def remaining_targeted_patterns(self, state: ProjectState, workspace: Path) -> list[DeprecatedPattern]:
        """Deprecated patterns the plan was responsible for, plus any the run introduced.

        A pattern is *targeted* when it lies in a file an applied plan step
        declared.  Patterns elsewhere that already existed at analysis time
        are out of the objective's scope and must not block SUCCESS.  Patterns
        absent from the analysis baseline were introduced by this run and
        always count, wherever they are.
        """
        current = scan_all_deprecated_patterns(workspace.resolve(), self.catalog)
        plan_files = {
            file for step in (state.plan.steps if state.plan else []) if step.status == "applied" for file in step.files
        }
        known = {
            (pattern.rule_id, pattern.file, pattern.snippet.strip())
            for pattern in (state.analysis.deprecated_patterns if state.analysis else [])
        }
        return [
            pattern
            for pattern in current
            if pattern.file in plan_files or (pattern.rule_id, pattern.file, pattern.snippet.strip()) not in known
        ]

    @staticmethod
    def _has_unapplied_plan_steps(state: ProjectState) -> bool:
        return bool(state.plan and any(step.status != "applied" for step in state.plan.steps))

    def _check_budget(self, state: ProjectState, started: float) -> None:
        if self.cancel_checker is not None and self.cancel_checker(state.run_id):
            Orchestrator.transition(state, TransitionSignal.CANCELLED)
            self._record(state, "orchestrator", "decision", "Run cancelled by API request.")
            raise WorkflowError("run cancelled")
        if monotonic() - started + state.metrics.wall_clock_s > state.budgets.max_wall_clock_s:
            Orchestrator.transition(state, TransitionSignal.BUDGET_EXCEEDED)
            self._record(state, "orchestrator", "decision", "Wall-clock budget exceeded.")
            raise WorkflowError("wall-clock budget exceeded")
        self._enforce_token_budget(state)

    def _report_failure(self, state: ProjectState, summary: str) -> None:
        state.stage = Stage.REPORT
        state.final_status = FinalStatus.FAILED
        self._record(state, "orchestrator", "error", summary)

    def _report_partial(self, state: ProjectState, summary: str, data: dict[str, object] | None = None) -> None:
        state.stage = Stage.REPORT
        state.final_status = FinalStatus.PARTIAL
        self._record(state, "orchestrator", "decision", summary, data)
