from pathlib import Path

import pytest

from reforge.backend.app.tools.sandbox import DockerSandbox, DockerUnavailable


FIXTURE_WORKSPACE = Path("reforge/backend/tests/fixtures/workspace")


def test_sandbox_requires_existing_workspace() -> None:
    with pytest.raises(ValueError, match="existing directory"):
        DockerSandbox(FIXTURE_WORKSPACE / "missing")


def test_sandbox_reports_unavailable_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = DockerSandbox(FIXTURE_WORKSPACE)

    def unavailable() -> object:
        raise DockerUnavailable("Docker is unavailable. Start Docker Desktop and build reforge-sandbox.")

    monkeypatch.setattr(sandbox, "_get_client", unavailable)

    with pytest.raises(DockerUnavailable, match="Docker is unavailable"):
        sandbox.run(["python", "-V"], phase="run", timeout=1)


def test_log_capture_preserves_combined_docker_output() -> None:
    class ContainerWithLogs:
        def logs(self, *, stdout: bool = True, stderr: bool = True) -> bytes:
            assert stdout is True
            assert stderr is True
            return b"standard output\nstandard error\n"

    stdout, stderr = DockerSandbox._read_logs(ContainerWithLogs())

    assert stdout == "standard output\nstandard error\n"
    assert stderr == ""


def test_compose_mount_source_uses_configured_host_runs_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFORGE_CONTAINER_RUNS_ROOT", str(FIXTURE_WORKSPACE.parent.resolve()))
    monkeypatch.setenv("REFORGE_DOCKER_HOST_RUNS_ROOT", "C:/host/runs")

    source = DockerSandbox(FIXTURE_WORKSPACE)._docker_mount_source()

    assert source == "C:/host/runs/workspace"


def test_sandbox_keeps_non_root_default_but_allows_compose_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DockerSandbox._sandbox_user() == "reforge"

    monkeypatch.setenv("REFORGE_SANDBOX_USER", "0")

    assert DockerSandbox._sandbox_user() == "0"


def test_sandbox_uses_tmp_home_and_disables_pip_cache() -> None:
    assert DockerSandbox._execution_environment() == {
        "HOME": "/tmp",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
