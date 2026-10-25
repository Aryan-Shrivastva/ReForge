from pathlib import Path

import pytest

from reforge.backend.app.tools.sandbox import (
    ROOT_FALLBACK_ENV,
    DockerSandbox,
    DockerUnavailable,
)


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


def test_sandbox_keeps_non_root_default_but_allows_explicit_root_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert DockerSandbox._sandbox_user() == "reforge"

    monkeypatch.setenv(ROOT_FALLBACK_ENV, "true")

    assert DockerSandbox._sandbox_user() == "0"
    assert ROOT_FALLBACK_ENV in caplog.text


def test_sandbox_uses_tmp_home_and_disables_pip_cache() -> None:
    assert DockerSandbox._execution_environment() == {
        "HOME": "/tmp",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "NPM_CONFIG_CACHE": "/tmp/npm-cache",
        "COREPACK_HOME": "/tmp/corepack",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "YARN_CACHE_FOLDER": "/tmp/yarn-cache",
        "npm_config_store_dir": "/tmp/pnpm-store",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    }


def test_sandbox_passes_all_hardening_options_to_docker() -> None:
    class FinishedContainer:
        id = "sandbox-id"
        attrs = {"State": {"Status": "exited", "ExitCode": 0}}

        def reload(self) -> None:
            pass

        def remove(self, *, force: bool = False) -> None:
            assert force is True

        def logs(self, *, stdout: bool = True, stderr: bool = True) -> bytes:
            return b"ok"

    class Containers:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def run(self, image: str, command: list[str], **kwargs: object) -> FinishedContainer:
            assert image == "reforge-sandbox:latest"
            assert command == ["python", "-V"]
            self.kwargs = kwargs
            return FinishedContainer()

    class Client:
        def __init__(self) -> None:
            self.containers = Containers()

    client = Client()
    result = DockerSandbox(FIXTURE_WORKSPACE, client=client).run(
        ["python", "-V"], phase="run", timeout=1
    )

    assert result.exit_code == 0
    assert client.containers.kwargs["read_only"] is True
    assert client.containers.kwargs["cap_drop"] == ["ALL"]
    assert client.containers.kwargs["security_opt"] == ["no-new-privileges"]
    assert client.containers.kwargs["pids_limit"] == 256
    assert client.containers.kwargs["nano_cpus"] == 2_000_000_000
    assert client.containers.kwargs["mem_limit"] == "2g"
    assert client.containers.kwargs["user"] == "reforge"


class _Container:
    id = "sandbox-id"

    def __init__(self, exit_code: int = 0, output: bytes = b"ok") -> None:
        self.attrs = {"State": {"Status": "exited", "ExitCode": exit_code}}
        self.output = output

    def reload(self) -> None:
        pass

    def remove(self, *, force: bool = False) -> None:
        pass

    def logs(self, *, stdout: bool = True, stderr: bool = True) -> bytes:
        return self.output


class _Client:
    """Fake Docker client: records runs and describes the backend's own container mounts."""

    def __init__(self, mounts: list[dict[str, str]] | None = None, container: _Container | None = None) -> None:
        self.runs: list[tuple[list[str], dict[str, object]]] = []
        self.mounts = mounts
        self.container = container or _Container()
        client = self

        class Containers:
            def run(self, image: str, command: list[str], **kwargs: object) -> _Container:
                client.runs.append((command, kwargs))
                return client.container

            def get(self, container_id: str):
                if client.mounts is None:
                    raise RuntimeError("no such container")
                return type("Self", (), {"attrs": {"Mounts": client.mounts}})()

        self.containers = Containers()


def test_compose_host_path_is_detected_from_the_backend_container_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs = tmp_path / "app" / "runs"
    workspace = runs / "run-1" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.delenv("REFORGE_DOCKER_HOST_RUNS_ROOT", raising=False)
    monkeypatch.setenv("REFORGE_CONTAINER_RUNS_ROOT", str(runs))
    monkeypatch.setattr(DockerSandbox, "_running_in_container", staticmethod(lambda: True))
    client = _Client(mounts=[
        {"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
        {"Source": "/home/dev/ReForge/runs", "Destination": str(runs)},
    ])

    source = DockerSandbox(workspace, client=client)._docker_mount_source()  # type: ignore[arg-type]

    assert source == "/home/dev/ReForge/runs/run-1/workspace"


def test_undetectable_compose_host_path_fails_with_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runs = tmp_path / "runs"
    workspace = runs / "run-2" / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.delenv("REFORGE_DOCKER_HOST_RUNS_ROOT", raising=False)
    monkeypatch.setenv("REFORGE_CONTAINER_RUNS_ROOT", str(runs))
    monkeypatch.setattr(DockerSandbox, "_running_in_container", staticmethod(lambda: True))

    with pytest.raises(DockerUnavailable, match="REFORGE_DOCKER_HOST_RUNS_ROOT"):
        DockerSandbox(workspace, client=_Client(mounts=None))._docker_mount_source()  # type: ignore[arg-type]


def test_sandbox_rejects_an_empty_or_wrong_workspace_mount(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    wrong_mount = _Container(exit_code=97, output=b"REFORGE_MOUNT_CHECK_FAILED\n")
    client = _Client(container=wrong_mount)

    with pytest.raises(DockerUnavailable, match="workspace mount"):
        DockerSandbox(tmp_path, client=client).run(["npm", "ci"], phase="install", timeout=5)  # type: ignore[arg-type]

    command, _ = client.runs[0]
    assert command[:2] == ["/bin/sh", "-c"]
    assert command[-2:] == ["npm", "ci"]  # the original command runs only after the check passes


def test_sandbox_mount_check_passes_through_real_results(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    client = _Client(container=_Container(exit_code=1, output=b"npm error: real failure"))

    result = DockerSandbox(tmp_path, client=client).run(["npm", "ci"], phase="install", timeout=5)  # type: ignore[arg-type]

    assert result.exit_code == 1
    assert "real failure" in result.stdout
    assert not any(path.name.startswith("reforge-mount") for path in (tmp_path / ".git").iterdir())
