"""Deterministic safety checks for repository mutations.

These guards run outside LLM prompts.  They reject unsafe proposals before a
patch is applied, so an agent cannot weaken a test suite to obtain a pass.
They cover Python (pytest/unittest) and JavaScript/TypeScript (Jest, Vitest,
Mocha) test conventions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..tools.languages import language_for, is_js_test_file, is_test_config, is_test_infrastructure


class GuardViolation(ValueError):
    """Raised when a proposed repository change violates a workflow guard."""


# ``--- a/path`` / ``+++ b/path``, optionally quoted by Git and optionally
# followed by a tab-separated timestamp.  Paths may contain spaces.
_DIFF_PATH = re.compile(r'^(?:---|\+\+\+) (?P<path>"(?:[^"\\]|\\.)*"|[^\t\r\n]+?)(?:\t.*)?\s*$')
_PYTHON_WEAKENING_MARKERS = (
    re.compile(r"\bpytest\.mark\.(?:skip|skipif|xfail)\b"),
    re.compile(r"\bpytest\.(?:skip|xfail|importorskip)\s*\("),
    re.compile(r"@(?:unittest\.)?(?:skip|skipIf|skipUnless|expectedFailure)\b"),
    re.compile(r"\bself\.skipTest\s*\("),
    re.compile(r"\braise\s+(?:unittest\.)?SkipTest\b"),
    re.compile(r"\bfrom\s+unittest\s+import\s+.*\b(?:skip|skipIf|skipUnless|expectedFailure)\b"),
)
_JS_WEAKENING_MARKERS = (
    re.compile(r"(?<![\w.$])(?:it|test|describe|suite|context|specify)\.(?:skip|todo|only|failing)\b"),
    # Bare globals only: ``model.fit(`` or ``obj.xit(`` are ordinary calls.
    re.compile(r"(?<![\w.$])(?:xit|xtest|xdescribe|xcontext|xspecify|fit|fdescribe)\s*\("),
    re.compile(r"(?<![\w.$])this\.skip\s*\("),
)
_BROAD_EXCEPT = re.compile(r"^\s*except(?:\s*:\s*|\s+(?:Exception|BaseException)\s*:)")
_PYTHON_ASSERTION = re.compile(r"^\s*(?:assert\b|self\.assert\w+\s*\()")
_JS_ASSERTION = re.compile(r"\b(?:expect|assert)\s*[.(]|\.should\b")
_PYTHON_SELECTION_WEAKENING = re.compile(
    r"--ignore|--deselect|(?:^|\s)-k(?:\s|=)|-p\s*no:|\b(?:testpaths|norecursedirs|python_files|python_classes|python_functions|collect_ignore)\b"
)
_JS_SELECTION_WEAKENING = re.compile(
    r"\b(?:testPathIgnorePatterns|testMatch|testRegex|roots|modulePathIgnorePatterns|passWithNoTests|testNamePattern)\b"
    r"|--(?:passWithNoTests|testPathIgnorePatterns|testNamePattern|grep|invert|exclude|bail)\b"
    r"|\|\|\s*(?:true|exit\s+0)\b|;\s*exit\s+0\b"
)
_VITEST_FILTER = re.compile(r"\b(?:exclude|include)\s*:")
# A version or range such as 1.1.4, ^5.1.0, >=3, ~2.0: the line declares a constraint.
_VERSION_LIKE = re.compile(r"\d+(?:\.[\dxX*]+)*")


def _unquote(path: str) -> str:
    """Decode Git's C-style quoted path (octal escapes for non-ASCII bytes)."""
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        try:
            return path[1:-1].encode("latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise GuardViolation(f"unsupported quoted patch path: {path!r}") from exc
    return path


def _normalise_path(path: str) -> str:
    """Normalise a diff path and reject absolute/traversal paths."""
    path = _unquote(path.strip())
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        path = path[2:]
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts or path in {"", "."} or "\\" in path:
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
        self._reject_protected_and_infrastructure(paths)

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
        infrastructure = sorted(path for path in normalised if is_test_infrastructure(path))
        if infrastructure:
            raise GuardViolation(f"codemod may not edit test infrastructure: {infrastructure}")

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
        self._reject_protected_and_infrastructure(paths)
        self._reject_test_weakening(unified_diff)
        self._reject_dependency_rollback(unified_diff)
        self.register_repair_patch(unified_diff)
        return paths

    def _reject_protected_and_infrastructure(self, paths: set[str]) -> None:
        protected = paths & self.protected_test_files
        if protected:
            raise GuardViolation(f"patch touches protected baseline tests: {sorted(protected)}")
        infrastructure = sorted(path for path in paths if is_test_infrastructure(path))
        if infrastructure:
            raise GuardViolation(f"patch touches test files or test infrastructure: {infrastructure}")

    def _reject_test_weakening(self, unified_diff: str) -> None:
        current_path: str | None = None
        for line in unified_diff.splitlines():
            if line.startswith("+++ ") or line.startswith("--- "):
                match = _DIFF_PATH.match(line)
                if line.startswith("+++ ") and match:
                    candidate = _normalise_path(match.group("path"))
                    current_path = None if candidate == "/dev/null" else candidate
                continue
            if line.startswith("+"):
                added = line[1:]
                language = language_for(current_path) if current_path else None
                markers = (
                    _PYTHON_WEAKENING_MARKERS if language == "python"
                    else _JS_WEAKENING_MARKERS if language in {"javascript", "typescript"}
                    else ()
                )
                if any(marker.search(added) for marker in markers):
                    raise GuardViolation("patch adds a test skip, xfail, todo, or focus marker")
                if language == "python" and _BROAD_EXCEPT.match(added):
                    raise GuardViolation("patch adds a broad exception handler")
                if current_path and is_test_config(current_path) and self._weakens_selection(current_path, added):
                    raise GuardViolation("patch weakens test selection in configuration")
            elif line.startswith("-"):
                removed = line[1:]
                language = language_for(current_path) if current_path else None
                if language in {"python", None} and _PYTHON_ASSERTION.match(removed):
                    raise GuardViolation("patch removes a test assertion")
                if current_path and is_js_test_file(current_path) and _JS_ASSERTION.search(removed):
                    raise GuardViolation("patch removes a test assertion")

    @staticmethod
    def _weakens_selection(path: str, added: str) -> bool:
        name = PurePosixPath(path).name
        if name in {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}:
            return bool(_PYTHON_SELECTION_WEAKENING.search(added))
        if _JS_SELECTION_WEAKENING.search(added):
            return True
        return name.startswith(("vitest.config.", "vite.config.")) and bool(_VITEST_FILTER.search(added))

    def _reject_dependency_rollback(self, unified_diff: str) -> None:
        """Reject a patch that replaces a dependency's newer constraint with its prior one.

        A rollback needs both halves in the same patch: a removed line naming
        the dependency at some *other* constraint (the upgrade being undone)
        and an added line naming it at the prior constraint.  Moving or
        copying a declaration at its unchanged version - consolidating
        manifests, moving a package to devDependencies - adds the prior
        constraint without removing a different one, and is not a rollback.
        Name and constraint are matched as whole tokens on the same line, so
        ``2.0`` never matches inside ``2.0.1``.
        """
        if self.allow_dependency_rollback_fallback:
            return
        lines = unified_diff.splitlines()
        added_lines = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
        removed_lines = [line[1:] for line in lines if line.startswith("-") and not line.startswith("---")]
        for dependency, old_constraint in self.prior_dependency_constraints.items():
            if not old_constraint:
                continue
            name_pattern = re.compile(
                r"(?<![\w.-])" + r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", dependency)) + r"(?![\w-])",
                re.IGNORECASE,
            )
            constraint_pattern = re.compile(r"(?<![\w.])" + re.escape(old_constraint) + r"(?![\w.])")
            restores_prior = any(name_pattern.search(line) and constraint_pattern.search(line) for line in added_lines)
            removes_other = any(
                name_pattern.search(line) and not constraint_pattern.search(line) and _VERSION_LIKE.search(line)
                for line in removed_lines
            )
            if restores_prior and removes_other:
                raise GuardViolation(
                    f"patch restores prior dependency constraint for {dependency!r}; rollback fallback is disabled"
                )
