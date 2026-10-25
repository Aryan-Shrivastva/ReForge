"""Host-safety guarantees: untrusted repositories never execute code through host-side Git."""

from __future__ import annotations

import io
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from reforge.backend.app.api.runs import ArchiveError, RunCreateRequest, RunService
from reforge.backend.app.tools.git_tools import GitTools
from reforge.backend.app.tools.safe_git import SafeGitError, initialize_repository, run_git
from reforge.backend.app.tools.sandbox import DockerSandbox


def make_zip(path: Path, entries: dict[str, str], *, symlinks: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, target)
    return path


def plain_git(workspace: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def repository_with_planted_hooks(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    initialize_repository(workspace, "baseline")
    marker = tmp_path / "pwned"
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho hook > '{marker.as_posix()}'\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    # A planted fsmonitor command runs on index refresh (git add/status/diff).
    plain_git(workspace, "config", "core.fsmonitor", f"echo fsmonitor > '{marker.as_posix()}'")
    return workspace, marker


def test_git_tools_never_run_repository_hooks_or_fsmonitor(tmp_path: Path) -> None:
    workspace, marker = repository_with_planted_hooks(tmp_path)
    git = GitTools(workspace)

    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git.diff()
    git.commit("change", ["app.py"])

    assert not marker.exists()


def test_git_tools_commits_without_machine_git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.js").write_text("module.exports = 1;\n", encoding="utf-8")
    initialize_repository(workspace, "baseline")
    # Match a URL clone, which has no local author identity configured.
    plain_git(workspace, "config", "--unset", "user.name")
    plain_git(workspace, "config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "missing-global-config"))
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)

    (workspace / "app.js").write_text("module.exports = 2;\n", encoding="utf-8")
    GitTools(workspace).commit("change", ["app.js"])

    author = run_git(["show", "-s", "--format=%an <%ae>"], cwd=workspace)
    assert author.stdout.strip() == "ReForge <reforge@local.invalid>"


def test_initialize_repository_refuses_existing_git_metadata(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(SafeGitError, match="existing .git"):
        initialize_repository(tmp_path, "baseline")


def test_run_git_disables_external_transports() -> None:
    completed = run_git(["ls-remote", "ext::sh -c touch% /tmp/reforge-ext-pwned"])

    assert completed.returncode != 0
    assert not Path("/tmp/reforge-ext-pwned").exists()


def test_archive_extraction_drops_git_metadata_and_strips_single_root(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "repo.zip",
        {
            "project-main/package.json": "{}",
            "project-main/src/index.js": "module.exports = 1;\n",
            "project-main/.git/config": "[core]\n\tfsmonitor = touch pwned\n",
            "project-main/.git/hooks/pre-commit": "#!/bin/sh\ntouch pwned\n",
        },
    )
    destination = tmp_path / "workspace"

    RunService._extract_archive(archive, destination)

    assert (destination / "package.json").is_file()
    assert (destination / "src" / "index.js").is_file()
    assert not (destination / ".git").exists()
    initialize_repository(destination, "Uploaded repository baseline")
    config = (destination / ".git" / "config").read_text(encoding="utf-8")
    assert "fsmonitor" not in config


@pytest.mark.parametrize(
    "name",
    ["../escape.py", "/absolute.py", "C:/windows.py", "dir\\..\\escape.py"],
)
def test_archive_extraction_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = make_zip(tmp_path / "bad.zip", {name: "x"})

    with pytest.raises(ArchiveError, match="unsafe path"):
        RunService._extract_archive(archive, tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


def test_archive_extraction_rejects_symlinks(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "link.zip", {"a.py": "x"}, symlinks={"secrets": "/etc/passwd"})

    with pytest.raises(ArchiveError, match="symbolic link"):
        RunService._extract_archive(archive, tmp_path / "workspace")


def test_archive_extraction_bounds_expanded_size_and_member_count(tmp_path: Path) -> None:
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("big.txt", "0" * 50_000)

    with pytest.raises(ArchiveError, match="expands beyond"):
        RunService._extract_archive(archive, tmp_path / "w1", max_bytes=10_000)
    with pytest.raises(ArchiveError, match="more than"):
        RunService._extract_archive(make_zip(tmp_path / "many.zip", {"a": "1", "b": "2"}), tmp_path / "w2", max_members=1)


def test_archive_extraction_rejects_non_zip_content(tmp_path: Path) -> None:
    archive = tmp_path / "fake.zip"
    archive.write_bytes(io.BytesIO(b"not a zip").getvalue())

    with pytest.raises(ArchiveError, match="not a valid ZIP"):
        RunService._extract_archive(archive, tmp_path / "workspace")


@pytest.mark.parametrize(
    "url",
    ["ext::sh -c id", "file:///etc", "/srv/repo", "-uhelp", "--upload-pack=touch x"],
)
def test_repository_urls_are_limited_to_network_transports(url: str) -> None:
    with pytest.raises(ValueError, match="repo_url"):
        RunCreateRequest(repo_url=url)


@pytest.mark.parametrize(
    "url",
    ["https://github.com/org/repo.git", "ssh://git@host/repo.git", "git@github.com:org/repo.git"],
)
def test_repository_urls_accept_standard_remotes(url: str) -> None:
    assert RunCreateRequest(repo_url=url).repo_url == url


def test_sandbox_mounts_git_metadata_read_only(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    volumes = DockerSandbox(tmp_path)._volumes()

    assert volumes[str(tmp_path.resolve())] == {"bind": "/work", "mode": "rw"}
    assert volumes[str(tmp_path.resolve() / ".git")] == {"bind": "/work/.git", "mode": "ro"}
