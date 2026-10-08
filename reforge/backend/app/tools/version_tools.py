"""Package-version lookup and dependency-resolution checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

from .interfaces import ExecResult, Sandbox


@dataclass(frozen=True, slots=True)
class VersionInfo:
    package: str
    latest: str


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    ok: bool
    output: str
    result: ExecResult


def query_latest_version(package: str, *, timeout_s: int = 10) -> VersionInfo:
    """Read the authoritative PyPI JSON metadata for a package's latest version."""
    url = f"https://pypi.org/pypi/{quote(package)}/json"
    try:
        with urlopen(url, timeout=timeout_s) as response:  # noqa: S310 - fixed PyPI host
            payload = json.loads(response.read().decode("utf-8"))
        version = payload["info"]["version"]
    except (URLError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to query PyPI for {package}: {exc}") from exc
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"PyPI returned no version for {package}")
    return VersionInfo(package=package, latest=version)


def check_resolution(
    sandbox: Sandbox, requirements: Iterable[str], *, timeout_s: int = 300
) -> ResolutionResult:
    """Ask pip to resolve candidate requirements in the sandbox without installing them."""
    requested = list(requirements)
    if not requested:
        raise ValueError("at least one requirement is required")
    result = sandbox.run(
        ["python", "-m", "pip", "install", "--dry-run", *requested],
        phase="install",
        timeout=timeout_s,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    return ResolutionResult(ok=result.exit_code == 0 and not result.timed_out, output=output, result=result)

