"""Sandboxed wrappers for deterministic, scoped source codemods."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from .interfaces import ExecResult, Sandbox
from .repo_tools import RepositoryTools


class Codemod(str, Enum):
    PYUPGRADE = "pyupgrade"
    RUFF = "ruff"
    BUMP_PYDANTIC = "bump_pydantic"


class CodemodTools:
    """Run only allow-listed codemods against explicit workspace-relative files."""

    def __init__(self, sandbox: Sandbox, workspace: Path, *, timeout_s: int = 300) -> None:
        self.sandbox = sandbox
        self.repository = RepositoryTools(workspace)
        self.timeout_s = timeout_s

    def run(self, codemod: Codemod, files: list[str]) -> ExecResult:
        if not files:
            raise ValueError("codemod requires at least one declared file")
        safe_files = [self.repository.resolve(path).relative_to(self.repository.workspace).as_posix() for path in files]
        if codemod is Codemod.PYUPGRADE:
            command = ["pyupgrade", "--py311-plus", *safe_files]
        elif codemod is Codemod.RUFF:
            command = ["ruff", "check", "--fix", *safe_files]
        elif codemod is Codemod.BUMP_PYDANTIC:
            command = ["bump-pydantic", *safe_files]
        else:  # pragma: no cover - Enum prevents this branch at runtime.
            raise ValueError(f"unsupported codemod: {codemod}")
        return self.sandbox.run(command, phase="run", timeout=self.timeout_s)
