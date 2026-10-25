"""Docker-only execution for target repositories.

ReForge itself runs on the host, but this module is the sole path for running
untrusted target-repository commands. Docker is imported lazily so contract
tests can run on machines where Docker Desktop is not installed or running.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Literal, Protocol

from .interfaces import ExecResult


LOGGER = logging.getLogger(__name__)
ROOT_FALLBACK_ENV = "REFORGE_SANDBOX_ROOT_FALLBACK"
_MOUNT_CHECK_EXIT = 97
_MOUNT_CHECK_MESSAGE = "REFORGE_MOUNT_CHECK_FAILED"
# Container path -> detected host path; the backend's own mounts never change at run time.
_HOST_PATH_CACHE: dict[str, str] = {}

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
        marker: Path | None = None
        try:
            volumes = self._volumes()
            command, marker = self._with_mount_check(cmd)
            container = client.containers.run(
                self.image,
                command,
                detach=True,
                working_dir="/work",
                volumes=volumes,
                network_mode=None if phase == "install" else "none",
                nano_cpus=self.cpus * 1_000_000_000,
                mem_limit=self.mem_limit,
                pids_limit=self.pids_limit,
                user=self._sandbox_user(),
                environment=self._execution_environment(),
                read_only=True,
                tmpfs={"/tmp": "rw,nosuid,nodev,size=512m"},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
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
            if marker is not None and exit_code == _MOUNT_CHECK_EXIT and _MOUNT_CHECK_MESSAGE in f"{stdout}{stderr}":
                raise DockerUnavailable(
                    f"The sandbox's /work is not this run's workspace mount (source {volumes and next(iter(volumes))}). "
                    "The Docker daemon cannot see the backend's runs directory; set REFORGE_DOCKER_HOST_RUNS_ROOT "
                    "to the absolute host path of ./runs."
                )
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
            if marker is not None:
                marker.unlink(missing_ok=True)
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

    def _volumes(self) -> dict[str, dict[str, str]]:
        """Mount the workspace writable but its Git metadata read-only.

        Host-side Git later commits sandbox-produced changes.  A writable
        ``.git`` would let repository code plant hooks, ``core.fsmonitor``
        commands, or filter drivers that the host would then execute.
        """
        source = self._docker_mount_source()
        volumes = {source: {"bind": "/work", "mode": "rw"}}
        if (self.workspace / ".git").exists():
            # A translated Compose host path is always POSIX-joined; a direct
            # local path uses the host's own separator.
            git_source = str(self.workspace / ".git") if source == str(self.workspace) else f"{source.rstrip('/')}/.git"
            volumes[git_source] = {"bind": "/work/.git", "mode": "ro"}
        return volumes

    def _docker_mount_source(self) -> str:
        """Translate a Compose-container workspace to its host bind source.

        Normal local runs bind ``workspace`` directly.  With Docker Compose,
        the backend itself is a container, while its Docker client talks to the
        host daemon through the socket.  The daemon needs the host path, not
        ``/app/runs/...`` inside the backend container: given a path that does
        not exist on the host, Docker silently creates an empty directory and
        the sandbox sees an empty ``/work``.

        ``REFORGE_DOCKER_HOST_RUNS_ROOT`` wins when set.  Otherwise, inside a
        container, the host path is read from the backend container's own
        mounts through the Docker API, so no manual configuration is needed.
        """
        container_runs_root = Path(os.getenv("REFORGE_CONTAINER_RUNS_ROOT", "/app/runs")).resolve()
        try:
            relative = self.workspace.relative_to(container_runs_root)
        except ValueError:
            return str(self.workspace)
        host_runs_root = os.getenv("REFORGE_DOCKER_HOST_RUNS_ROOT", "").strip()
        if not host_runs_root:
            if not self._running_in_container():
                return str(self.workspace)
            host_runs_root = self._detect_host_path(container_runs_root)
        normalized_host_root = host_runs_root.rstrip("/\\")
        return f"{normalized_host_root}/{relative.as_posix()}"

    def _detect_host_path(self, container_path: Path) -> str:
        """Find the host source of ``container_path`` from this container's own bind mounts."""
        cache_key = str(container_path)
        if cache_key in _HOST_PATH_CACHE:
            return _HOST_PATH_CACHE[cache_key]
        try:
            own = self._get_client().containers.get(socket.gethostname())  # type: ignore[attr-defined]
            mounts = own.attrs.get("Mounts", [])
        except Exception as exc:  # Docker SDK exceptions vary by version.
            raise DockerUnavailable(
                "Could not determine the host path of the runs directory from the backend container "
                f"({exc}). Set REFORGE_DOCKER_HOST_RUNS_ROOT to the absolute host path of ./runs."
            ) from exc
        target = container_path.as_posix()
        best: tuple[str, str] | None = None
        for mount in mounts if isinstance(mounts, list) else []:
            destination = str(mount.get("Destination", "")).replace("\\", "/").rstrip("/")
            source = str(mount.get("Source", ""))
            if not destination or not source:
                continue
            if target == destination or target.startswith(f"{destination}/"):
                if best is None or len(destination) > len(best[0]):
                    best = (destination, source)
        if best is None:
            raise DockerUnavailable(
                f"The backend container has no bind mount covering {target}. "
                "Set REFORGE_DOCKER_HOST_RUNS_ROOT to the absolute host path of ./runs."
            )
        destination, source = best
        host_path = source.rstrip("/\\") + target[len(destination):]
        _HOST_PATH_CACHE[cache_key] = host_path
        LOGGER.info("Detected host path %s for sandbox mounts of %s.", host_path, target)
        return host_path

    @staticmethod
    def _running_in_container() -> bool:
        return Path("/.dockerenv").exists()

    def _with_mount_check(self, cmd: list[str]) -> tuple[list[str], Path | None]:
        """Prefix ``cmd`` with a check that ``/work`` really is this workspace.

        A one-time token is written into ``.git`` (invisible to Git and
        mounted read-only in the sandbox).  If the container cannot read it,
        ``/work`` is empty or the wrong directory, and the command exits with
        a distinctive code instead of failing in a misleading way (for
        example ``npm ci`` reporting a missing lockfile).
        """
        git_directory = self.workspace / ".git"
        if not git_directory.is_dir():
            return cmd, None
        token = uuid.uuid4().hex
        marker = git_directory / f"reforge-mount-{token}"
        marker.write_text(token, encoding="utf-8")
        check = (
            f'[ "$(cat /work/.git/{marker.name} 2>/dev/null)" = "{token}" ] || '
            f'{{ echo "{_MOUNT_CHECK_MESSAGE}" >&2; exit {_MOUNT_CHECK_EXIT}; }}; exec "$@"'
        )
        return ["/bin/sh", "-c", check, "reforge-sandbox", *cmd], marker

    @staticmethod
    def _sandbox_user() -> str:
        """Return the unprivileged sandbox user unless the explicit fallback is set.

        Docker Desktop file sharing can reject writes from the image's regular
        UID on some Windows hosts.  That compatibility exception is deliberately
        opt-in; it does not make arbitrary UID selection part of the public
        configuration surface.
        """
        fallback = os.getenv(ROOT_FALLBACK_ENV, "").strip().lower()
        if fallback in {"1", "true", "yes", "on"}:
            LOGGER.warning(
                "%s is enabled; sandbox commands run as root for Docker bind-mount compatibility.",
                ROOT_FALLBACK_ENV,
            )
            return "0"
        return "reforge"

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
            "NPM_CONFIG_CACHE": "/tmp/npm-cache",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "COREPACK_HOME": "/tmp/corepack",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "YARN_CACHE_FOLDER": "/tmp/yarn-cache",
            "npm_config_store_dir": "/tmp/pnpm-store",
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
