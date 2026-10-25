"""Baseline-only characterization test generation for Python and JavaScript/TypeScript."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal

from ..core.state import TestGenerationProposal
from ..llm import LLMClient
from ..tools.languages import (
    JS_LIKE_EXTENSIONS,
    detect_js_test_framework,
    is_skipped,
    is_test_file,
    package_dependency_names,
    read_package_json,
)
from ..tools.repo_tools import RepositoryTools
from .base import StructuredAgent
from .executor import AgentExecutor
from .prompts import JS_TEST_GENERATION_PROMPT, TEST_GENERATION_PROMPT

GENERATED_DIRECTORY = "tests/reforge_generated/"
# Bounded source context keeps one generation call within a predictable token budget.
_MAX_SOURCE_CHARS = 60_000

_FRAMEWORK_NAMES = {"jest": "Jest", "vitest": "Vitest", "node": "Node.js built-in test runner (node:test)"}
_FRAMEWORK_NOTES = {
    "jest": "Jest globals (describe, it, expect) are available without imports. ",
    "vitest": "Import describe, it, and expect from 'vitest'. ",
    "node": (
        "Use only Node built-ins: load the runner with require('node:test') (or import test from 'node:test') and "
        "assertions from 'node:assert/strict'; do not use Jest, Vitest, Mocha, or any npm test package. "
        "Many application modules connect to a database or start a server when loaded: only load modules whose "
        "top level is side-effect free (pure helpers, validators, formatters, data transforms), and skip route, "
        "server, database, and browser DOM scripts entirely. "
    ),
}


class CharacterizationPathError(ValueError):
    """Raised when generated tests fall outside their dedicated baseline directory."""


class CharacterizationTestGenerator(StructuredAgent):
    """Ask an LLM for test text; deterministic code restricts where it can be written."""

    tool_names = ("list_tree", "read_file", "write_new_file")

    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        executor: AgentExecutor | None = None,
        repository_factory: Callable[[Path], RepositoryTools] = RepositoryTools,
        max_retries: int = 2,
    ) -> None:
        if (llm is None) == (executor is None):
            raise ValueError("provide exactly one of llm or executor")
        self.executor = executor
        self.repository_factory = repository_factory
        if llm is not None:
            super().__init__(llm, max_retries=max_retries)

    def propose(
        self,
        objective: str,
        workspace: Path,
        *,
        previous_failures: list[dict[str, str]] | None = None,
    ) -> TestGenerationProposal:
        repository = self.repository_factory(workspace)
        files = [path for path in repository.list_tree() if not is_skipped(path)]
        failure_context = json.dumps(previous_failures or [], ensure_ascii=False)
        language = self._language(workspace) or "python"
        framework = self._generation_framework(workspace) if language == "javascript" else None
        extension = ".py" if language == "python" else self._js_extension(workspace, framework)
        if language == "python":
            prompt = TEST_GENERATION_PROMPT.format(
                objective=objective,
                files=json.dumps(files),
                source=json.dumps(self._source(repository, files, (".py",))),
                previous_failures=failure_context,
                schema=json.dumps(TestGenerationProposal.model_json_schema()),
            )
        else:
            package = read_package_json(workspace)
            prompt = JS_TEST_GENERATION_PROMPT.format(
                objective=objective,
                framework=_FRAMEWORK_NAMES[framework or "node"],
                framework_note=_FRAMEWORK_NOTES[framework or "node"],
                module_system="ES modules (import/export)" if package.get("type") == "module" else "CommonJS (require/module.exports)",
                extension=extension,
                files=json.dumps(files),
                source=json.dumps(self._source(repository, files, JS_LIKE_EXTENSIONS)),
                previous_failures=failure_context,
                schema=json.dumps(TestGenerationProposal.model_json_schema()),
            )
        if self.executor is not None:
            proposal = self.executor.run(
                role="Baseline characterization-test generator",
                goal="Capture original observable behavior with deterministic new tests.",
                backstory="You never alter existing tests and only write fresh baseline tests.",
                prompt=prompt,
                expected_output="One TestGenerationProposal JSON object matching the requested schema.",
                output_model=TestGenerationProposal,
            )
        else:
            proposal = self.complete_model(prompt, TestGenerationProposal)
        self._validate_paths(proposal, language, extension)
        return proposal

    def supports(self, workspace: Path) -> bool:
        """Return whether this generator can write native, executable tests for the stack.

        Python projects get pytest files.  JavaScript projects get tests for
        their own Jest or Vitest setup, and otherwise tests for Node's built-in
        ``node:test`` runner, which needs no dependency, so a project with no
        test framework at all can still be given a behavioral baseline.  Other
        stacks are not given synthetic tests: Python filesystem checks for a
        Java or Kotlin project are neither native tests nor useful coverage.
        """
        language = self._language(workspace)
        if language is None:
            return False
        repository = self.repository_factory(workspace)
        extensions = (".py",) if language == "python" else JS_LIKE_EXTENSIONS
        return any(
            path.endswith(extensions) and not is_test_file(path) and not path.startswith("tests/")
            for path in repository.list_tree()
            if not is_skipped(path)
        )

    def write(self, workspace: Path, proposal: TestGenerationProposal) -> list[str]:
        """Create proposed files once; callers test and retain or drop them."""
        language = self._language(workspace) or "python"
        extension = ".py" if language == "python" else self._js_extension(workspace, self._generation_framework(workspace))
        self._validate_paths(proposal, language, extension)
        repository = self.repository_factory(workspace)
        paths: list[str] = []
        for file in proposal.files:
            repository.write_new_file(file.path, file.content)
            paths.append(file.path)
        return paths

    @staticmethod
    def _language(workspace: Path) -> Literal["python", "javascript"] | None:
        # Decide this before walking the tree. npm may have created POSIX
        # symlinks in node_modules on a Windows bind mount, which are unsafe to
        # inspect from the host filesystem.
        if (workspace / "package.json").is_file():
            return "javascript"
        return "python"

    @staticmethod
    def _generation_framework(workspace: Path) -> str:
        """Jest/Vitest when the project uses one (its discovery collects the files), else node:test."""
        framework = detect_js_test_framework(workspace)
        return framework if framework in {"jest", "vitest"} else "node"

    @staticmethod
    def _js_extension(workspace: Path, framework: str | None) -> str:
        """TypeScript tests only where the runner executes TypeScript natively or via ts-jest."""
        package = read_package_json(workspace)
        dependencies = package_dependency_names(package)
        typescript = (workspace / "tsconfig.json").is_file() and "typescript" in dependencies
        if typescript and (framework == "vitest" or "ts-jest" in dependencies):
            return ".test.ts"
        if package.get("type") == "module" and framework == "jest":
            return ".test.mjs"
        return ".test.js"

    @staticmethod
    def _source(repository: RepositoryTools, files: list[str], extensions: tuple[str, ...]) -> dict[str, str]:
        source: dict[str, str] = {}
        remaining = _MAX_SOURCE_CHARS
        for path in files:
            if remaining <= 0:
                break
            if not path.endswith(extensions) or is_test_file(path) or path.startswith("tests/") or ".min." in path:
                continue
            try:
                content = repository.read_file(path)
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            source[path] = content[:remaining]
            remaining -= len(source[path])
        return source

    @staticmethod
    def _validate_paths(
        proposal: TestGenerationProposal,
        language: str = "python",
        extension: str = ".py",
    ) -> None:
        for file in proposal.files:
            path = PurePosixPath(file.path)
            if not file.path.startswith(GENERATED_DIRECTORY) or path.is_absolute() or ".." in path.parts:
                raise CharacterizationPathError(f"generated test path is unsafe: {file.path!r}")
            if not file.path.endswith(extension):
                raise CharacterizationPathError(
                    f"generated {language} test must end with {extension!r}: {file.path!r}"
                )
