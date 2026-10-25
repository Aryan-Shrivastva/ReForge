"""Safe, deterministic file operations confined to a run workspace."""

from __future__ import annotations

import re
from pathlib import Path

from .safe_git import run_git


class WorkspaceViolation(ValueError):
    """Raised when a requested path escapes the assigned workspace."""


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<suffix>.*)$"
)


def normalise_unified_diff_hunks(unified_diff: str) -> str:
    """Correct only unified-diff hunk counts emitted inconsistently by an LLM.

    Models often provide exact changed lines and context but miscount a hunk's
    ``-old,+new`` lengths, which Git reports as a corrupt patch before it can
    evaluate the actual change.  Recalculate those two count fields from the
    hunk body.  File paths, line content, and starting offsets are untouched;
    ``git apply --check`` remains the authority for applicability and every
    mutation still passes ReForge's guards.
    """
    lines = unified_diff.splitlines(keepends=True)
    normalised: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _HUNK_HEADER.match(line.rstrip("\r\n"))
        if match is None:
            normalised.append(line)
            index += 1
            continue

        end = index + 1
        old_count = 0
        new_count = 0
        while end < len(lines):
            body = lines[end]
            if body.startswith("@@ ") or body.startswith("diff --git "):
                break
            if (
                body.startswith("--- ")
                and end + 1 < len(lines)
                and lines[end + 1].startswith("+++ ")
            ):
                break
            if body.startswith("-"):
                old_count += 1
            elif body.startswith("+"):
                new_count += 1
            elif body.startswith(" "):
                old_count += 1
                new_count += 1
            elif body.startswith("\\"):
                pass
            else:
                # Let Git reject malformed hunk bodies; do not reinterpret
                # arbitrary text as context.
                break
            end += 1

        suffix = match.group("suffix")
        old_range = f"{match.group('old_start')},{old_count}" if old_count != 1 else match.group("old_start")
        new_range = f"{match.group('new_start')},{new_count}" if new_count != 1 else match.group("new_start")
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        normalised.append(f"@@ -{old_range} +{new_range} @@{suffix}{newline}")
        normalised.extend(lines[index + 1 : end])
        index = end
    return "".join(normalised)


class RepositoryTools:
    """Repository inspection and patching without executing repository code."""

    _IGNORED_DIRECTORIES = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".reforge-artifacts",
    }

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")

    def resolve(self, path: str | Path) -> Path:
        """Resolve a user/agent path and reject traversal outside the workspace."""
        candidate = Path(path)
        resolved = (self.workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {path}") from exc
        return resolved

    def list_tree(self) -> list[str]:
        """List source paths deterministically, excluding generated/tool directories."""
        files: list[str] = []
        for path in self.workspace.rglob("*"):
            if any(part in self._IGNORED_DIRECTORIES for part in path.relative_to(self.workspace).parts):
                continue
            if path.is_file():
                files.append(path.relative_to(self.workspace).as_posix())
        return sorted(files)

    def read_file(self, path: str | Path) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise FileNotFoundError(target)
        return target.read_text(encoding="utf-8")

    def search_code(self, pattern: str, *, glob: str = "*.py") -> list[str]:
        """Return `path:line:text` matches for a regular expression."""
        expression = re.compile(pattern)
        matches: list[str] = []
        for path in self.workspace.rglob(glob):
            if any(part in self._IGNORED_DIRECTORIES for part in path.relative_to(self.workspace).parts):
                continue
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(self.workspace).as_posix()
            matches.extend(
                f"{relative}:{number}:{line}"
                for number, line in enumerate(lines, start=1)
                if expression.search(line)
            )
        return matches

    def write_new_file(self, path: str | Path, content: str) -> Path:
        """Create a new UTF-8 file; overwriting an existing file is forbidden."""
        target = self.resolve(path)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def apply_patch(self, unified_diff: str) -> None:
        """Apply a unified diff through Git, failing rather than partially applying it."""
        patch_bytes = normalise_unified_diff_hunks(unified_diff).encode("utf-8")
        checked = run_git(["apply", "--check", "--whitespace=error", "--"], cwd=self.workspace, input=patch_bytes, text=False)
        if checked.returncode != 0:
            raise ValueError(f"patch rejected: {checked.stderr.decode('utf-8', errors='replace').strip()}")
        completed = run_git(["apply", "--whitespace=error", "--"], cwd=self.workspace, input=patch_bytes, text=False)
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"patch check passed but apply failed: {error}")
