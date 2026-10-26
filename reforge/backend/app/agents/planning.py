"""Planning agent and deterministic plan validation."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from ..core.state import AnalysisReport, Plan, RepairAttempt, TestReport
from ..llm import LLMClient
from .base import StructuredAgent
from .executor import AgentExecutor
from .prompts import PLANNING_PROMPT, REPLANNING_PROMPT


class PlanValidationError(ValueError):
    """Raised when an LLM plan violates ReForge's deterministic plan contract."""


def validate_plan(plan: Plan) -> None:
    """Require relative, workspace-safe paths and an acyclic dependency graph."""
    steps = {step.id: step for step in plan.steps}
    for step in plan.steps:
        for dependency in step.depends_on:
            if dependency not in steps:
                raise PlanValidationError(f"{step.id} depends on unknown step {dependency}")
        for file in step.files:
            path = PurePosixPath(file)
            if path.is_absolute() or ".." in path.parts or file == ".":
                raise PlanValidationError(f"{step.id} includes unsafe path {file!r}")

    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise PlanValidationError("plan dependencies contain a cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in steps[step_id].depends_on:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in steps:
        visit(step_id)


class PlanningAgent(StructuredAgent):
    tool_names = ("read_file", "search_code")

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        executor: AgentExecutor | None = None,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (executor is None):
            raise ValueError("provide exactly one of llm or executor")
        self.executor = executor
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def run(
        self,
        objective: str,
        analysis: AnalysisReport,
        workspace_files: list[str],
        *,
        reviewer_feedback: str | None = None,
        previous_plan: Plan | None = None,
    ) -> Plan:
        prompt = PLANNING_PROMPT.format(
            objective=objective,
            analysis=json.dumps(analysis.model_dump(mode="json")),
            files=json.dumps(workspace_files),
            schema=json.dumps(Plan.model_json_schema()),
        )
        if reviewer_feedback:
            # A person reviewed the previous proposal and asked for changes.
            previous = json.dumps(previous_plan.model_dump(mode="json")) if previous_plan else "none"
            prompt += (
                "\nA human reviewer rejected the previous proposal and asked for changes. Their feedback takes "
                "priority over your own preferences; follow it unless it would require editing tests or weakening "
                f"verification.\nReviewer feedback: {reviewer_feedback}\nPrevious proposal: {previous}\n"
            )
        if self.executor is not None:
            return self.executor.run(
                role="Repository modernization planner",
                goal="Produce a safe, dependency-aware modernization plan.",
                backstory="You create small acyclic plans and never modify files yourself.",
                prompt=prompt,
                expected_output="One Plan JSON object matching the requested schema.",
                output_model=Plan,
                validator=validate_plan,
            )
        return self.complete_model(prompt, Plan, validate_plan)

    def replan(
        self,
        objective: str,
        analysis: AnalysisReport,
        workspace_files: list[str],
        previous_plan: Plan,
        repairs: list[RepairAttempt],
        latest_report: TestReport,
    ) -> Plan:
        """Produce a materially informed plan after repair progress has stopped."""
        prompt = REPLANNING_PROMPT.format(
            objective=objective,
            analysis=json.dumps(analysis.model_dump(mode="json")),
            files=json.dumps(workspace_files),
            previous_plan=json.dumps(previous_plan.model_dump(mode="json")),
            repairs=json.dumps([repair.model_dump(mode="json") for repair in repairs]),
            failures=json.dumps(latest_report.model_dump(mode="json")),
            schema=json.dumps(Plan.model_json_schema()),
        )
        if self.executor is not None:
            return self.executor.run(
                role="Repository modernization replanner",
                goal="Replace an ineffective plan with a safe, evidence-driven alternative.",
                backstory="You learn from concrete test failures and do not repeat failed work.",
                prompt=prompt,
                expected_output="One Plan JSON object matching the requested schema.",
                output_model=Plan,
                validator=validate_plan,
            )
        return self.complete_model(prompt, Plan, validate_plan)
