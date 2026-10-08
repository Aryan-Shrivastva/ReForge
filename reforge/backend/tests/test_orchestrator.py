from reforge.backend.app.core.orchestrator import Orchestrator, TransitionSignal
from reforge.backend.app.core.state import FinalStatus, RepairAttempt, Stage
from reforge.backend.tests.conftest import make_state


def test_happy_path_transition_table() -> None:
    state = make_state()
    for signal in (
        TransitionSignal.INITIALIZED,
        TransitionSignal.ANALYSIS_VALID,
        TransitionSignal.PLAN_VALID,
        TransitionSignal.BASELINE_USABLE,
        TransitionSignal.TRANSFORM_COMPLETE,
        TransitionSignal.TEST_PASSED,
        TransitionSignal.REPORT_WRITTEN,
    ):
        Orchestrator.transition(state, signal)

    assert state.stage is Stage.DONE
    assert state.final_status is FinalStatus.SUCCESS


def test_budget_exhaustion_routes_to_partial_when_repair_improved() -> None:
    state = make_state()
    state.stage = Stage.TEST
    state.repairs = [
        RepairAttempt(
            iteration=1,
            diagnosis="Fix API compatibility",
            root_cause_category="api_change",
            failing_before=3,
            failing_after=1,
        )
        for _ in range(state.budgets.max_repairs)
    ]

    Orchestrator.transition(state, TransitionSignal.TEST_FAILED)

    assert state.stage is Stage.REPORT
    assert state.final_status is FinalStatus.PARTIAL


def test_cancellation_aborts_from_any_stage() -> None:
    state = make_state(stage=Stage.PLAN)

    Orchestrator.transition(state, TransitionSignal.CANCELLED)

    assert state.stage is Stage.REPORT
    assert state.final_status is FinalStatus.ABORTED

