"""Baseline-only characterization test generation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from ..core.state import TestGenerationProposal
from ..llm import LLMClient
from ..tools.repo_tools import RepositoryTools
from .base import StructuredAgent
from .crewai_adapter import CrewAIExecutor
from .prompts import TEST_GENERATION_PROMPT


class CharacterizationPathError(ValueError):
    """Raised when generated tests fall outside their dedicated baseline directory."""


class CharacterizationTestGenerator(StructuredAgent):
    """Ask an LLM for test text; deterministic code restricts where it can be written."""

    tool_names = ("list_tree", "read_file", "write_new_file")

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        crew_executor: CrewAIExecutor | None = None,
        repository_factory: Callable[[Path], RepositoryTools] = RepositoryTools,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (crew_executor is None):
            raise ValueError("provide exactly one of llm or crew_executor")
        self.crew_executor = crew_executor
        self.repository_factory = repository_factory
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def propose(self, objective: str, workspace: Path) -> TestGenerationProposal:
        repository = self.repository_factory(workspace)
        files = repository.list_tree()
        source = {
            path: repository.read_file(path)
            for path in files
            if path.endswith(".py") and not path.startswith("tests/")
        }
        prompt = TEST_GENERATION_PROMPT.format(
            objective=objective,
            files=json.dumps(files),
            source=json.dumps(source),
            schema=json.dumps(TestGenerationProposal.model_json_schema()),
        )
        if self.crew_executor is not None:
            proposal = self.crew_executor.run(
                role="Baseline characterization-test generator",
                goal="Capture original observable behavior with deterministic new tests.",
                backstory="You never alter existing tests and only write fresh baseline tests.",
                prompt=prompt,
                expected_output="One TestGenerationProposal JSON object matching the requested schema.",
                output_model=TestGenerationProposal,
            )
        else:
            proposal = self.complete_model(prompt, TestGenerationProposal)
        self._validate_paths(proposal)
        return proposal

    def supports(self, workspace: Path) -> bool:
        """Return whether this generator can write executable tests for the stack.

        This implementation writes pytest files only.  It must not create
        Python filesystem checks for JavaScript, Java, or Kotlin projects,
        because those checks are neither native tests nor useful behavior
        coverage.
        """
        # Decide this before walking the tree. npm may have created POSIX
        # symlinks in node_modules on a Windows bind mount, and those are both
        # irrelevant to Python test generation and unsafe to inspect from the
        # host filesystem.
        if (workspace / "package.json").is_file():
            return False
        repository = self.repository_factory(workspace)
        return any(
            path.endswith(".py") and not path.startswith("tests/")
            for path in repository.list_tree()
        )

    def write(self, workspace: Path, proposal: TestGenerationProposal) -> list[str]:
        """Create proposed files once; callers test and retain or drop them."""
        self._validate_paths(proposal)
        repository = self.repository_factory(workspace)
        paths: list[str] = []
        for file in proposal.files:
            repository.write_new_file(file.path, file.content)
            paths.append(file.path)
        return paths

    @staticmethod
    def _validate_paths(proposal: TestGenerationProposal) -> None:
        for file in proposal.files:
            path = PurePosixPath(file.path)
            if not file.path.startswith("tests/reforge_generated/") or path.is_absolute() or ".." in path.parts:
                raise CharacterizationPathError(f"generated test path is unsafe: {file.path!r}")
