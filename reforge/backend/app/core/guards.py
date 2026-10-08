"""Deterministic safety checks for repository mutations.

These guards run outside LLM prompts.  They reject unsafe proposals before a
patch is applied, so an agent cannot weaken a test suite to obtain a pass.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


class GuardViolation(ValueError):
    """Raised when a proposed repository change violates a workflow guard."""


_DIFF_PATH = re.compile(r"^(?:---|\+\+\+)\s+(?P<path>\S+)")
_WEAKENING_MARKERS = (
    re.compile(r"\bpytest\.mark\.(?:skip|xfail)\b"),
    re.compile(r"@unittest\.skip\b"),
)
_BROAD_EXCEPT = re.compile(r"^\s*except(?:\s*:\s*|\s+(?:Exception|BaseException)\s*:)")
_ASSERTION = re.compile(r"^\s*(?:assert\b|self\.assert\w+\s*\()")
_TEST_CONFIG_NAMES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}


def _normalise_path(path: str) -> str:
    """Normalise a diff path and reject absolute/traversal paths."""
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        path = path[2:]
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or path in {"", "."}:
        raise GuardViolation(f"unsafe patch path: {path!r}")
    return candidate.as_posix()


def changed_paths(unified_diff: str) -> set[str]:
    """Extract all repository paths mentioned by a unified diff."""
    paths: set[str] = set()
    for line in unified_diff.splitlines():
        match = _DIFF_PATH.match(line)
        if match:
            path = _normalise_path(match.group("path"))
            if path != "/dev/null":
                paths.add(path)
    if not paths:
        raise GuardViolation("patch does not contain any unified-diff file paths")
    return paths


def _new_file_paths(unified_diff: str) -> set[str]:
    """Return files introduced by a ``--- /dev/null`` unified-diff header."""
    lines = unified_diff.splitlines()
    created: set[str] = set()
    for index, line in enumerate(lines[:-1]):
        if line.startswith("--- /dev/null"):
            next_match = _DIFF_PATH.match(lines[index + 1])
            if next_match and lines[index + 1].startswith("+++"):
                created.add(_normalise_path(next_match.group("path")))
    return created


@dataclass(slots=True)
class MutationGuards:
    """Validate mutation proposals against one ReForge run's frozen rules."""

    protected_test_files: set[str] = field(default_factory=set)
    prior_dependency_constraints: dict[str, str] = field(default_factory=dict)
    allow_dependency_rollback_fallback: bool = False
    _repair_patch_hashes: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.protected_test_files = {_normalise_path(path) for path in self.protected_test_files}

    def validate_transformation_patch(self, unified_diff: str, declared_files: list[str]) -> set[str]:
        """Reject protected, weakening, or out-of-scope changes before applying."""
        paths = changed_paths(unified_diff)
        protected = paths & self.protected_test_files
        if protected:
            raise GuardViolation(f"patch touches protected baseline tests: {sorted(protected)}")

        allowed = {_normalise_path(path) for path in declared_files}
        new_files = _new_file_paths(unified_diff)
        out_of_scope = paths - allowed - new_files
        if out_of_scope:
            raise GuardViolation(f"patch touches files outside the plan step: {sorted(out_of_scope)}")

        self._reject_test_weakening(unified_diff)
        self._reject_dependency_rollback(unified_diff)
        return paths

    def validate_codemod_files(self, files: list[str]) -> None:
        """A codemod may only receive declared, non-protected source files."""
        normalised = {_normalise_path(path) for path in files}
        protected = normalised & self.protected_test_files
        if protected:
            raise GuardViolation(f"codemod may not edit protected baseline tests: {sorted(protected)}")

    def register_repair_patch(self, unified_diff: str) -> str:
        """Reject an exact repair diff that was already attempted in this run."""
        digest = hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()
        if digest in self._repair_patch_hashes:
            raise GuardViolation("duplicate repair patch rejected")
        self._repair_patch_hashes.add(digest)
        return digest

    def validate_repair_patch(self, unified_diff: str) -> set[str]:
        """Validate a repair without granting it a transformation step's scope."""
        paths = changed_paths(unified_diff)
        protected = paths & self.protected_test_files
        if protected:
            raise GuardViolation(f"patch touches protected baseline tests: {sorted(protected)}")
        self._reject_test_weakening(unified_diff)
        self._reject_dependency_rollback(unified_diff)
        self.register_repair_patch(unified_diff)
        return paths

    def _reject_test_weakening(self, unified_diff: str) -> None:
        current_path: str | None = None
        for line in unified_diff.splitlines():
            if line.startswith("+++ "):
                match = _DIFF_PATH.match(line)
                current_path = _normalise_path(match.group("path")) if match else None
                continue
            if line.startswith("+") and not line.startswith("+++"):
                added = line[1:]
                if any(marker.search(added) for marker in _WEAKENING_MARKERS):
                    raise GuardViolation("patch adds a test skip or xfail marker")
                if _BROAD_EXCEPT.match(added):
                    raise GuardViolation("patch adds a broad exception handler")
                if current_path and PurePosixPath(current_path).name in _TEST_CONFIG_NAMES:
                    if "--ignore" in added or re.search(r"(?:^|\s)-k(?:\s|=)", added):
                        raise GuardViolation("patch weakens test selection in configuration")
            if line.startswith("-") and not line.startswith("---") and _ASSERTION.match(line[1:]):
                raise GuardViolation("patch removes a test assertion")

    def _reject_dependency_rollback(self, unified_diff: str) -> None:
        if self.allow_dependency_rollback_fallback:
            return
        added = "\n".join(
            line[1:] for line in unified_diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        for dependency, old_constraint in self.prior_dependency_constraints.items():
            if old_constraint and old_constraint in added:
                raise GuardViolation(
                    f"patch restores prior dependency constraint for {dependency!r}; rollback fallback is disabled"
                )
