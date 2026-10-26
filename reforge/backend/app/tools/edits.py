"""Exact search/replace edits: the reliable way for an LLM to change a file.

Unified diffs written by a model routinely miscount lines or misquote context,
so ``git apply`` rejects them.  An edit instead quotes a snippet of the
current file and its replacement.  ReForge requires the snippet to occur
exactly once, computes the new contents itself, derives the unified diff for
the mutation guards, and only then writes the files.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from ..core.state import FileEdit
from .repo_tools import RepositoryTools


class EditError(ValueError):
    """Raised when an edit cannot be applied unambiguously."""


@dataclass(frozen=True, slots=True)
class EditPlan:
    """New contents per file, plus the unified diff the guards validate."""

    contents: dict[str, str]
    created: set[str]
    diff: str


def plan_edits(repository: RepositoryTools, edits: list[FileEdit]) -> EditPlan:
    """Resolve edits against current file contents without writing anything."""
    if not edits:
        raise EditError("no edits were proposed")
    originals: dict[str, str | None] = {}
    contents: dict[str, str] = {}
    for edit in edits:
        path = _normalise(edit.path)
        if path not in originals:
            target = repository.resolve(path)
            originals[path] = _read_exact(target) if target.is_file() else None
            contents[path] = originals[path] or ""
        current = contents[path]
        if edit.search == "":
            if originals[path] is not None and current.strip():
                raise EditError(f"{path} already exists; quote the text to replace instead of creating it")
            contents[path] = edit.replace
            continue
        if originals[path] is None:
            raise EditError(f"{path} does not exist; use an empty search to create it")
        contents[path] = _replace_once(path, current, edit.search, edit.replace)

    diff_parts: list[str] = []
    created: set[str] = set()
    for path, new in contents.items():
        old = originals[path]
        if old == new:
            continue
        if old is None:
            created.add(path)
        diff_parts.append(_unified_diff(path, old, new))
    if not diff_parts:
        raise EditError("the edits do not change any file")
    return EditPlan(contents={path: contents[path] for path in contents if originals[path] != contents[path]}, created=created, diff="".join(diff_parts))


def write_edits(repository: RepositoryTools, plan: EditPlan) -> list[str]:
    """Write planned contents (after the guards have accepted ``plan.diff``)."""
    for path, content in plan.contents.items():
        target = repository.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" keeps the line endings the content already uses.
        target.write_text(content, encoding="utf-8", newline="")
    return sorted(plan.contents)


def _replace_once(path: str, text: str, search: str, replace: str) -> str:
    count = text.count(search)
    if count == 1:
        return text.replace(search, replace, 1)
    # Models usually quote with "\n"; tolerate files that use CRLF.
    if "\r\n" in text and "\r\n" not in search:
        crlf_search = search.replace("\n", "\r\n")
        if text.count(crlf_search) == 1:
            return text.replace(crlf_search, replace.replace("\n", "\r\n"), 1)
    if count == 0:
        hint = _closest_line_hint(text, search)
        raise EditError(f"search text not found in {path}{hint}; copy it exactly from the current file")
    raise EditError(f"search text matches {count} places in {path}; include more surrounding lines so it is unique")


def _closest_line_hint(text: str, search: str) -> str:
    first = next((line.strip() for line in search.splitlines() if line.strip()), "")
    if not first:
        return ""
    candidates = difflib.get_close_matches(first, [line.strip() for line in text.splitlines()], n=1, cutoff=0.6)
    return f" (closest line in the file: {candidates[0]!r})" if candidates else ""


def _unified_diff(path: str, old: str | None, new: str) -> str:
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    # Terminate a final line without newline so diff headers stay on their own lines.
    return "".join(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n" for line in lines)


def _read_exact(path: Path) -> str:
    """Read without newline translation so CRLF files keep their line endings."""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _normalise(path: str) -> str:
    return Path(path.strip()).as_posix().removeprefix("./")
