"""Transformation Agent: typed patch proposals plus deterministic application."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..core.guards import GuardViolation, MutationGuards
from ..core.state import PlanStep, TransformationProposal, TransformationResult
from ..llm import LLMClient
from ..tools.codemod_tools import Codemod, CodemodTools
from ..tools.git_tools import GitTools
from ..tools.repo_tools import RepositoryTools, normalise_unified_diff_hunks
from .base import StructuredAgent
from .crewai_adapter import CrewAIExecutor
from .prompts import TRANSFORMATION_PROMPT


class TransformationAgent(StructuredAgent):
    """Propose, guard, apply, and checkpoint one declared modernization step."""

    tool_names = (
        "read_file",
        "apply_patch",
        "write_new_file",
        "update_dependency_file",
        "run_codemod",
        "git_commit",
        "git_diff",
    )

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        crew_executor: CrewAIExecutor | None = None,
        max_retries: int = 2,
        repository_factory: Callable[[Path], RepositoryTools] = RepositoryTools,
        git_factory: Callable[[Path], GitTools] = GitTools,
    ) -> None:
        if (llm is None) == (crew_executor is None):
            raise ValueError("provide exactly one of llm or crew_executor")
        self.crew_executor = crew_executor
        self.repository_factory = repository_factory
        self.git_factory = git_factory
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def run(
        self,
        *,
        objective: str,
        step: PlanStep,
        workspace: Path,
        guards: MutationGuards,
        codemods: CodemodTools | None = None,
    ) -> TransformationResult:
        """Apply one bounded step, retrying a rejected manual patch once."""
        repository = self.repository_factory(workspace)
        git = self.git_factory(workspace)
        prompt = self._prompt(objective, step, repository, guards)
        retry_note = ""

        for patch_attempt in range(2):
            proposal = self._complete_proposal(f"{prompt}{retry_note}")
            try:
                return self._apply_proposal(step, proposal, repository, git, guards, codemods)
            except (GuardViolation, ValueError, RuntimeError) as exc:
                if patch_attempt == 1 or proposal.codemod != "none":
                    return TransformationResult(
                        step_id=step.id,
                        status="failed",
                        error=str(exc),
                    )
                retry_note = (
                    "\n\nThe previous patch was rejected by deterministic guards or could not apply. "
                    f"Produce a corrected proposal. Error: {exc}"
                )

        raise AssertionError("two transformation attempts should always return")  # pragma: no cover

    def _prompt(
        self,
        objective: str,
        step: PlanStep,
        repository: RepositoryTools,
        guards: MutationGuards,
    ) -> str:
        files: dict[str, str] = {}
        for path in step.files:
            try:
                files[path] = repository.read_file(path)
            except FileNotFoundError:
                files[path] = "<new file: no current contents>"
        return TRANSFORMATION_PROMPT.format(
            objective=objective,
            step=json.dumps(step.model_dump(mode="json")),
            protected_test_files=json.dumps(sorted(guards.protected_test_files)),
            files=json.dumps(files),
            schema=json.dumps(TransformationProposal.model_json_schema()),
        )

    def _complete_proposal(self, prompt: str) -> TransformationProposal:
        if self.crew_executor is not None:
            return self.crew_executor.run(
                role="Repository modernization transformer",
                goal="Produce a minimal, scoped modernization patch.",
                backstory="You never alter tests and only propose patches; deterministic tools apply them.",
                prompt=prompt,
                expected_output="One TransformationProposal JSON object matching the requested schema.",
                output_model=TransformationProposal,
            )
        return self.complete_model(prompt, TransformationProposal)

    @staticmethod
    def _apply_proposal(
        step: PlanStep,
        proposal: TransformationProposal,
        repository: RepositoryTools,
        git: GitTools,
        guards: MutationGuards,
        codemods: CodemodTools | None,
    ) -> TransformationResult:
        if proposal.codemod != "none":
            if codemods is None:
                raise ValueError("proposal selected a codemod but no codemod runner is configured")
            guards.validate_codemod_files(step.files)
            result = codemods.run(Codemod(proposal.codemod), step.files)
            if result.exit_code != 0 or result.timed_out:
                raise RuntimeError(result.stderr or result.stdout or "codemod failed")
            diff = git.diff()
            if not diff:
                return TransformationResult(step_id=step.id, status="skipped")
            files_changed = sorted(guards.validate_transformation_patch(diff, step.files))
        elif proposal.patches:
            unified_diff = normalise_unified_diff_hunks(
                "\n".join(patch.unified_diff.rstrip() for patch in proposal.patches) + "\n"
            )
            files_changed = sorted(guards.validate_transformation_patch(unified_diff, step.files))
            repository.apply_patch(unified_diff)
            # ``git diff`` omits an untracked file created by a valid patch;
            # the applied unified diff is therefore the authoritative record.
            diff = git.diff() or unified_diff
        else:
            return TransformationResult(step_id=step.id, status="skipped")

        commit_sha = git.commit(f"{step.id}: {step.title}", files_changed)
        return TransformationResult(
            step_id=step.id,
            status="applied",
            files_changed=files_changed,
            commit_sha=commit_sha,
            diff=diff,
        )
