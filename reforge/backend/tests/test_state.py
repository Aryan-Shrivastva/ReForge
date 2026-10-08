import pytest
from pydantic import ValidationError

from reforge.backend.app.core.state import Plan, PlanStep


def test_plan_rejects_duplicate_step_ids() -> None:
    step = {
        "id": "S1",
        "title": "One",
        "description": "Do one thing",
        "kind": "other",
        "validation_hint": "Tests pass",
    }

    with pytest.raises(ValidationError, match="unique"):
        Plan(version=1, steps=[step, step], rationale="A plan")


def test_plan_step_requires_standard_step_identifier() -> None:
    with pytest.raises(ValidationError):
        PlanStep(
            id="one",
            title="One",
            description="Do one thing",
            kind="other",
            validation_hint="Tests pass",
        )

