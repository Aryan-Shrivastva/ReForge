"""Repair Agent: proposal, deterministic guard, application, and checkpoint."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..core.guards import GuardViolation, MutationGuards, changed_paths
from ..core.state import Plan, RepairAttempt, RepairProposal, TestReport
from ..llm import LLMClient
from ..tools.git_tools import GitTools
from ..tools.edits import plan_edits, write_edits
from ..tools.repo_tools import RepositoryTools, normalise_unified_diff_hunks
from .base import StructuredAgent
from .executor import AgentExecutor
from .prompts import REPAIR_PROMPT


class RepairAgent(StructuredAgent):
    """Turn a failing report into one small, guarded repair commit."""

    tool_names = (
        "read_file", "search_code", "apply_patch", "update_dependency_file", "git_commit", "git_diff",
        "lookup_rule",
    )

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        executor: AgentExecutor | None = None,
        repository_factory: Callable[[Path], RepositoryTools] = RepositoryTools,
        git_factory: Callable[[Path], GitTools] = GitTools,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (executor is None):
            raise ValueError("provide exactly one of llm or executor")
        self.executor = executor
        self.repository_factory = repository_factory
        self.git_factory = git_factory
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def run(
        self,
        *,
        objective: str,
        plan: Plan,
        report: TestReport,
        repairs: list[RepairAttempt],
        workspace: Path,
        guards: MutationGuards,
        baseline_commit: str,
        failing_before: int,
    ) -> RepairAttempt:
        repository = self.repository_factory(workspace)
        git = self.git_factory(workspace)
        files = repository.list_tree()
        diff = git.diff(baseline_commit)
        prompt = REPAIR_PROMPT.format(
            objective=objective,
            plan=json.dumps(plan.model_dump(mode="json")),
            report=json.dumps(report.model_dump(mode="json")),
            repairs=json.dumps([repair.model_dump(mode="json") for repair in repairs]),
            diff=diff,
            protected_test_files=json.dumps(sorted(guards.protected_test_files)),
            files=json.dumps(files),
            source=json.dumps(self._source_context(repository, plan, report, guards, diff)),
            schema=json.dumps(RepairProposal.model_json_schema()),
        )
        retry_note = ""
        last_error: Exception | None = None

        for patch_attempt in range(2):
            proposal = self._complete_proposal(f"{prompt}{retry_note}")
            try:
                if proposal.edits:
                    # Preferred: exact search/replace edits, validated via their derived diff.
                    edit_plan = plan_edits(repository, proposal.edits)
                    paths = sorted(guards.validate_repair_patch(edit_plan.diff))
                    write_edits(repository, edit_plan)
                else:
                    unified_diff = normalise_unified_diff_hunks(
                        "\n".join(patch.unified_diff.rstrip() for patch in proposal.patches) + "\n"
                    )
                    paths = sorted(guards.validate_repair_patch(unified_diff))
                    repository.apply_patch(unified_diff)
                iteration = len(repairs) + 1
                commit = git.commit(f"R{iteration}: {proposal.diagnosis}", paths)
                return RepairAttempt(
                    iteration=iteration,
                    diagnosis=proposal.diagnosis,
                    root_cause_category=proposal.root_cause_category,
                    files_changed=paths,
                    commit_sha=commit,
                    failing_before=failing_before,
                )
            except GuardViolation:
                # Safety-policy violations are not formatting failures and must
                # never be retried as a way to negotiate around the guard.
                raise
            except (ValueError, RuntimeError) as exc:
                last_error = exc
                if patch_attempt == 1:
                    raise
                retry_note = (
                    "\n\nThe previous repair was rejected before any change was committed. "
                    "Return new edits whose search text is copied exactly from the current source contents above. "
                    f"Rejection: {exc}"
                )

        raise AssertionError(f"repair patch attempts exhausted: {last_error}")  # pragma: no cover

    @staticmethod
    def _source_context(
        repository: RepositoryTools,
        plan: Plan,
        report: TestReport,
        guards: MutationGuards,
        diff: str,
    ) -> dict[str, str]:
        """Supply bounded, deterministic source context for an exact repair diff.

        Repair proposals previously received only filenames and a diff. That
        encourages a model to invent hunk context after a prior transformation.
        Include current contents for plan targets, changed files, failing test
        files, and frozen tests (read-only) so the model can make an applicable
        patch without being authorized to edit those tests.
        """
        paths = {path for step in plan.steps for path in step.files}
        try:
            paths.update(changed_paths(diff))
        except ValueError:
            # A Git-produced diff should be valid; retain the safe plan/test
            # context if a third-party Git implementation emits an odd header.
            pass
        paths.update(guards.protected_test_files)
        paths.update(
            failure.file
            for failure in [*report.failed, *report.errored]
            if failure.file
        )

        contents: dict[str, str] = {}
        remaining = 24_000
        for path in sorted(paths):
            if remaining <= 0:
                break
            try:
                content = repository.read_file(path)
            except (FileNotFoundError, UnicodeDecodeError):
                continue
            snippet = content[:remaining]
            contents[path] = snippet
            remaining -= len(snippet)
        return contents

    def _complete_proposal(self, prompt: str) -> RepairProposal:
        if self.executor is not None:
            return self.executor.run(
                role="Repository modernization repairer",
                goal="Fix a concrete regression without weakening frozen tests.",
                backstory="You group failures by root cause and propose one small safe patch.",
                prompt=prompt,
                expected_output="One RepairProposal JSON object matching the requested schema.",
                output_model=RepairProposal,
            )
        return self.complete_model(prompt, RepairProposal)
