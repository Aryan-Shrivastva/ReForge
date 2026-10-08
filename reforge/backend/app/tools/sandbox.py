"""Docker-only execution for target repositories.

ReForge itself runs on the host, but this module is the sole path for running
untrusted target-repository commands. Docker is imported lazily so contract
tests can run on machines where Docker Desktop is not installed or running.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Literal, Protocol

from .interfaces import ExecResult


class DockerContainer(Protocol):
    id: str

    def reload(self) -> None: ...

    @property
    def attrs(self) -> dict[str, object]: ...

    def kill(self) -> None: ...

    def remove(self, *, force: bool = False) -> None: ...

    def logs(self, *, stdout: bool = True, stderr: bool = True) -> bytes: ...


class DockerClient(Protocol):
    class Containers(Protocol):
        def run(self, image: str, command: list[str], **kwargs: object) -> DockerContainer: ...

    containers: Containers

    class API(Protocol):
        def logs(self, container: str, **kwargs: object) -> tuple[bytes, bytes]: ...

    api: API


class DockerUnavailable(RuntimeError):
    """Raised when Docker cannot be used to execute repository code."""


class DockerSandbox:
    """Run one command in one constrained, disposable Docker container."""

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = "reforge-sandbox:latest",
        cpus: int = 2,
        mem_limit: str = "2g",
        pids_limit: int = 256,
        client: DockerClient | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")
        if cpus < 1 or pids_limit < 1:
            raise ValueError("cpus and pids_limit must be positive")
        self.image = image
        self.cpus = cpus
        self.mem_limit = mem_limit
        self.pids_limit = pids_limit
        self._client = client

    def run(
        self, cmd: list[str], phase: Literal["install", "run"], timeout: int
    ) -> ExecResult:
        """Execute `cmd` under Docker limits, enforcing the given wall-clock timeout."""
        if not cmd:
            raise ValueError("cmd must not be empty")
        if timeout < 1:
            raise ValueError("timeout must be positive")
        client = self._get_client()
        started = time.monotonic()
        container: DockerContainer | None = None
        timed_out = False
        try:
            container = client.containers.run(
                self.image,
                cmd,
                detach=True,
                working_dir="/work",
                volumes={self._docker_mount_source(): {"bind": "/work", "mode": "rw"}},
                network_mode=None if phase == "install" else "none",
                nano_cpus=self.cpus * 1_000_000_000,
                mem_limit=self.mem_limit,
                pids_limit=self.pids_limit,
                user=self._sandbox_user(),
                environment=self._execution_environment(),
                read_only=True,
                tmpfs={"/tmp": "rw,nosuid,nodev,size=512m"},
            )
            while self._is_running(container):
                if time.monotonic() - started >= timeout:
                    timed_out = True
                    container.kill()
                    break
                time.sleep(0.1)
            container.reload()
            state = container.attrs.get("State", {})
            exit_code = int(state.get("ExitCode", 1)) if isinstance(state, dict) else 1
            stdout, stderr = self._read_logs(container)
            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_s=time.monotonic() - started,
                timed_out=timed_out,
            )
        except DockerUnavailable:
            raise
        except Exception as exc:  # Docker SDK exceptions vary by installed version.
            raise DockerUnavailable(f"Docker sandbox failed to start: {exc}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass

    def _get_client(self) -> DockerClient:
        if self._client is not None:
            return self._client
        try:
            import docker

            client = docker.from_env()
            client.ping()
        except Exception as exc:
            raise DockerUnavailable(
                "Docker is unavailable. Start Docker Desktop and build reforge-sandbox."
            ) from exc
        self._client = client
        return client

    def _docker_mount_source(self) -> str:
        """Translate a Compose-container workspace to its host bind source.

        Normal local runs bind ``workspace`` directly.  With Docker Compose,
        the backend itself is a container, while its Docker client talks to the
        host daemon through the socket.  The daemon needs the host path, not
        ``/app/runs/...`` inside the backend container.  Compose supplies the
        explicit host runs root only in that deployment mode.
        """
        host_runs_root = os.getenv("REFORGE_DOCKER_HOST_RUNS_ROOT", "").strip()
        container_runs_root = os.getenv("REFORGE_CONTAINER_RUNS_ROOT", "/app/runs")
        if not host_runs_root:
            return str(self.workspace)
        try:
            relative = self.workspace.relative_to(Path(container_runs_root).resolve())
        except ValueError:
            return str(self.workspace)
        normalized_host_root = host_runs_root.rstrip("/\\")
        return f"{normalized_host_root}/{relative.as_posix()}"

    @staticmethod
    def _sandbox_user() -> str:
        """Return the sandbox UID, retaining the non-root default from the spec."""
        return os.getenv("REFORGE_SANDBOX_USER", "reforge")

    @staticmethod
    def _execution_environment() -> dict[str, str]:
        """Keep package-manager state out of the image's read-only home directory.

        The disposable container has a writable ``/tmp`` tmpfs and a writable
        repository mount.  Pip may otherwise use ``/root/.cache`` when the
        Docker Desktop compatibility UID is root, which correctly fails under
        our read-only-root-filesystem guard.  Disabling pip's cache preserves
        that guard while allowing source-only dependencies to build in the
        virtual environment stored under ``/work``.
        """
        return {
            "HOME": "/tmp",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }

    @staticmethod
    def _is_running(container: DockerContainer) -> bool:
        container.reload()
        state = container.attrs.get("State", {})
        return isinstance(state, dict) and state.get("Status") == "running"

    @staticmethod
    def _read_logs(container: DockerContainer) -> tuple[str, str]:
        """Return Docker's combined output when the engine cannot demultiplex it.

        Docker SDK 7's low-level ``logs`` API does not accept ``demux``. The
        high-level API reliably returns both streams in chronological order but
        combines them, so preserve all diagnostics in stdout rather than lose
        stderr content. Test-report parsing consumes the combined tail.
        """
        try:
            output = container.logs(stdout=True, stderr=True)
            return output.decode(errors="replace"), ""
        except Exception:
            return "", "Unable to retrieve container logs."
