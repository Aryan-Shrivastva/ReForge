from pathlib import Path
import subprocess

import pytest

from reforge.backend.app.tools.repo_tools import (
    RepositoryTools,
    WorkspaceViolation,
    normalise_unified_diff_hunks,
)


FIXTURE_WORKSPACE = Path("reforge/backend/tests/fixtures/dependency_repo")


def test_lists_reads_and_searches_workspace_files() -> None:
    tools = RepositoryTools(FIXTURE_WORKSPACE)

    assert "requirements.txt" in tools.list_tree()
    assert "Flask==1.1.4" in tools.read_file("requirements.txt")
    assert tools.search_code("REQUIRES", glob="setup.py") == [
        "setup.py:3:REQUIRES = [\"click>=8\"]",
        "setup.py:4:setup(name=\"fixture\", install_requires=REQUIRES)",
    ]


def test_list_tree_ignores_node_modules(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.ts").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
    (tmp_path / "node_modules" / ".bin" / "tool").write_text("generated\n", encoding="utf-8")

    files = RepositoryTools(tmp_path).list_tree()

    assert files == ["src/main.ts"]


def test_rejects_workspace_escape() -> None:
    tools = RepositoryTools(FIXTURE_WORKSPACE)

    with pytest.raises(WorkspaceViolation):
        tools.resolve("../../outside.py")


def test_apply_patch_preserves_lf_bytes_for_git(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    patch = "--- a/setup.py\n+++ b/setup.py\n@@ -1 +1 @@\n-old\n+new\n"

    RepositoryTools(FIXTURE_WORKSPACE).apply_patch(patch)

    assert len(calls) == 2
    assert all(call["input"] == patch.encode("utf-8") for call in calls)
    assert all(not call.get("text") for call in calls)


def test_normalise_unified_diff_hunks_corrects_only_miscounted_lengths() -> None:
    patch = "--- a/setup.py\n+++ b/setup.py\n@@ -1,1 +1,1 @@\n-old\n+new\n+extra\n"

    normalised = normalise_unified_diff_hunks(patch)

    assert "@@ -1 +1,2 @@" in normalised
    assert "-old\n+new\n+extra\n" in normalised
