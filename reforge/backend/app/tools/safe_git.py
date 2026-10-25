"""Host-side Git invocation that never executes repository-controlled programs.

Target repositories are untrusted.  Every Git command ReForge runs on the host
goes through :func:`run_git`, which disables hooks, ``core.fsmonitor``
commands, external transports, and external diff drivers on the command line.
Command-line configuration takes precedence over repository-local
configuration, so a planted ``.git/config`` cannot re-enable them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


SAFE_GIT_CONFIG: tuple[str, ...] = (
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
    "-c", "protocol.ext.allow=never",
    "-c", "submodule.recurse=false",
)

_UNSAFE_ENVIRONMENT = ("GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND", "GIT_DIR", "GIT_WORK_TREE")


class SafeGitError(RuntimeError):
    """Raised when a safe Git helper cannot complete."""


def safe_git_environment() -> dict[str, str]:
    """Return the process environment without Git program overrides or prompts."""
    environment = {key: value for key, value in os.environ.items() if key not in _UNSAFE_ENVIRONMENT}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def run_git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    input: bytes | str | None = None,
    timeout: float | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run one Git command with repository-controlled code execution disabled."""
    return subprocess.run(
        ["git", *SAFE_GIT_CONFIG, *args],
        cwd=cwd,
        input=input,
        text=text,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=safe_git_environment(),
    )


def initialize_repository(workspace: Path, message: str) -> None:
    """Create a fresh repository with ReForge-owned configuration and one baseline commit.

    ``git init`` keeps an existing ``.git`` directory's configuration and
    hooks, so an extracted archive must never contain one.
    """
    if (workspace / ".git").exists():
        raise SafeGitError("refusing to initialize over an existing .git directory")
    for command in (
        ["init"],
        ["config", "user.email", "reforge@local.invalid"],
        ["config", "user.name", "ReForge"],
        ["add", "."],
        ["commit", "--no-verify", "-m", message],
    ):
        completed = run_git(command, cwd=workspace)
        if completed.returncode != 0:
            raise SafeGitError(completed.stderr.strip() or completed.stdout.strip() or "could not initialize repository")
