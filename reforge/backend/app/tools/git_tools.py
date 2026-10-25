"""Deterministic Git checkpoints for a ReForge workspace."""

from __future__ import annotations

from pathlib import Path

from .repo_tools import RepositoryTools
from .safe_git import run_git


class GitToolError(RuntimeError):
    """Raised when Git cannot perform a requested workspace operation."""


class GitTools:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        RepositoryTools(self.workspace)
        self._run("rev-parse", "--is-inside-work-tree")
        top_level = Path(self._run("rev-parse", "--show-toplevel")).resolve()
        if top_level != self.workspace:
            raise GitToolError(
                "workspace must be the root of its own Git repository; nested repositories are not allowed"
            )

    def current_commit(self) -> str:
        return self._run("rev-parse", "HEAD")

    def create_branch(self, branch: str) -> None:
        if not branch.startswith("reforge/"):
            raise ValueError("ReForge work branches must start with 'reforge/'")
        self._run("switch", "-c", branch)

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        """Stage declared paths (or all current changes) and create a checkpoint."""
        if not message.strip():
            raise ValueError("commit message must not be blank")
        if paths:
            checked = [str(RepositoryTools(self.workspace).resolve(path)) for path in paths]
            self._run("add", "--", *checked)
        else:
            self._run("add", "-A")
        self._run("commit", "--no-verify", "-m", message)
        return self.current_commit()

    def diff(self, revision: str | None = None) -> str:
        args = ("diff", "--no-ext-diff", "--no-textconv", revision) if revision else ("diff", "--no-ext-diff", "--no-textconv")
        return self._run(*args)

    def checkout(self, revision: str) -> None:
        self._run("checkout", revision)

    def reset_to(self, revision: str) -> None:
        """Restore this isolated ReForge work branch to a recorded good checkpoint."""
        if not revision.strip():
            raise ValueError("revision must not be blank")
        self._run("reset", "--hard", revision)

    def _run(self, *args: str) -> str:
        completed = run_git(list(args), cwd=self.workspace)
        if completed.returncode != 0:
            raise GitToolError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout.strip()
