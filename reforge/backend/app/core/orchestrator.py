"""Pure, deterministic routing rules for the ReForge workflow.

This M0 implementation deliberately contains no LLM or Docker calls. It is the
testable routing contract that later milestones wire to real agents and tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .state import FinalStatus, ProjectState, Stage, TestReport


class TransitionSignal(str, Enum):
    INITIALIZED = "initialized"
    ANALYSIS_VALID = "analysis_valid"
    ANALYSIS_FAILED = "analysis_failed"
    PLAN_VALID = "plan_valid"
    BASELINE_USABLE = "baseline_usable"
    BASELINE_UNUSABLE = "baseline_unusable"
    TRANSFORM_COMPLETE = "transform_complete"
    TEST_PASSED = "test_passed"
    TEST_FAILED = "test_failed"
    REPAIR_APPLIED = "repair_applied"
    REPAIR_NO_PROGRESS = "repair_no_progress"
    REPLAN_VALID = "replan_valid"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"
    REPORT_WRITTEN = "report_written"


@dataclass(frozen=True, slots=True)
class Transition:
    next_stage: Stage
    final_status: FinalStatus | None = None


class InvalidTransition(ValueError):
    """Raised when a signal is not permitted for the current workflow stage."""


class Orchestrator:
    """Transition-table implementation, independent of agent implementations."""

    _TABLE: dict[tuple[Stage, TransitionSignal], Transition] = {
        (Stage.INIT, TransitionSignal.INITIALIZED): Transition(Stage.ANALYZE),
        (Stage.ANALYZE, TransitionSignal.ANALYSIS_VALID): Transition(Stage.PLAN),
        (Stage.ANALYZE, TransitionSignal.ANALYSIS_FAILED): Transition(
            Stage.REPORT, FinalStatus.FAILED
        ),
        (Stage.PLAN, TransitionSignal.PLAN_VALID): Transition(Stage.BASELINE),
        (Stage.BASELINE, TransitionSignal.BASELINE_USABLE): Transition(Stage.TRANSFORM),
        (Stage.BASELINE, TransitionSignal.BASELINE_UNUSABLE): Transition(
            Stage.REPORT, FinalStatus.FAILED
        ),
        (Stage.TRANSFORM, TransitionSignal.TRANSFORM_COMPLETE): Transition(Stage.TEST),
        (Stage.TEST, TransitionSignal.TEST_PASSED): Transition(
            Stage.REPORT, FinalStatus.SUCCESS
        ),
        (Stage.TEST, TransitionSignal.TEST_FAILED): Transition(Stage.REPAIR),
        (Stage.REPAIR, TransitionSignal.REPAIR_APPLIED): Transition(Stage.TEST),
        (Stage.REPAIR, TransitionSignal.REPAIR_NO_PROGRESS): Transition(Stage.REPLAN),
        (Stage.REPLAN, TransitionSignal.REPLAN_VALID): Transition(Stage.TRANSFORM),
        (Stage.REPORT, TransitionSignal.REPORT_WRITTEN): Transition(Stage.DONE),
    }

    @classmethod
    def transition(cls, state: ProjectState, signal: TransitionSignal) -> ProjectState:
        """Apply one allowed transition, enforcing global abort signals first."""
        if signal is TransitionSignal.CANCELLED:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED
            return state
        if signal is TransitionSignal.BUDGET_EXCEEDED:
            state.stage = Stage.REPORT
            state.final_status = FinalStatus.ABORTED
            return state

        if signal is TransitionSignal.TEST_FAILED and state.stage is Stage.TEST:
            if len(state.repairs) >= state.budgets.max_repairs:
                state.stage = Stage.REPORT
                state.final_status = cls._failure_status(state)
                return state

        if signal is TransitionSignal.REPAIR_NO_PROGRESS and state.stage is Stage.REPAIR:
            if state.replans >= state.budgets.max_replans:
                state.stage = Stage.REPORT
                state.final_status = cls._failure_status(state)
                return state

        transition = cls._TABLE.get((state.stage, signal))
        if transition is None:
            raise InvalidTransition(f"{signal.value} is invalid from {state.stage.value}")
        state.stage = transition.next_stage
        if transition.final_status is not None:
            state.final_status = transition.final_status
        return state

    @staticmethod
    def failure_count(report: TestReport, baseline_pass_set: list[str]) -> int:
        """Count failing regression-guard tests only, per Section 7."""
        protected = set(baseline_pass_set)
        return sum(
            failure.test_id in protected for failure in [*report.failed, *report.errored]
        )

    @staticmethod
    def _failure_status(state: ProjectState) -> FinalStatus:
        """Use PARTIAL only when a repair improved the transform-start failure count."""
        attempts = [attempt for attempt in state.repairs if attempt.failing_after is not None]
        if any(attempt.failing_after < attempt.failing_before for attempt in attempts):
            return FinalStatus.PARTIAL
        return FinalStatus.FAILED

