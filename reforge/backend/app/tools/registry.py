"""Package-registry lookups (PyPI and npm): an external web API exposed as an MCP tool.

Answers "what is the latest published version, and is the package deprecated?"
from the registries themselves, so the analysis can ground upgrade targets in
real data instead of the model's memory.
"""

from __future__ import annotations

import json
import re
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import BaseModel

Ecosystem = Literal["pypi", "npm"]

_PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}$")
_TIMEOUT_S = 10
_USER_AGENT = "ReForge-registry-lookup/1.0"


class RegistryLookup(BaseModel):
    """Validated result of one registry lookup (the MCP tool's return type)."""

    ecosystem: Ecosystem
    package: str
    found: bool
    latest_version: str | None = None
    deprecated: str | None = None
    source_url: str
    error: str | None = None


def lookup_latest_version(ecosystem: str, package: str, *, opener=urlopen) -> RegistryLookup:
    """Query PyPI or npm for a package's latest version and deprecation notice."""
    if ecosystem not in ("pypi", "npm"):
        raise ValueError("ecosystem must be 'pypi' or 'npm'")
    package = package.strip()
    pattern = _PYPI_NAME if ecosystem == "pypi" else _NPM_NAME
    if not pattern.match(package):
        raise ValueError(f"not a valid {ecosystem} package name: {package!r}")
    if ecosystem == "pypi":
        url = f"https://pypi.org/pypi/{quote(package)}/json"
    else:
        url = f"https://registry.npmjs.org/{quote(package, safe='@')}/latest"
    try:
        with opener(Request(url, headers={"Accept": "application/json", "User-Agent": _USER_AGENT}), timeout=_TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return RegistryLookup(ecosystem=ecosystem, package=package, found=False, source_url=url)  # type: ignore[arg-type]
        return RegistryLookup(ecosystem=ecosystem, package=package, found=False, source_url=url, error=f"HTTP {exc.code}")  # type: ignore[arg-type]
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return RegistryLookup(ecosystem=ecosystem, package=package, found=False, source_url=url, error=str(exc)[:200])  # type: ignore[arg-type]

    if ecosystem == "pypi":
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        version = info.get("version")
        classifiers = info.get("classifiers") or []
        inactive = next((item for item in classifiers if "Development Status :: 7 - Inactive" in str(item)), None)
        deprecated = "Development Status: 7 - Inactive" if inactive else None
    else:
        version = payload.get("version") if isinstance(payload, dict) else None
        flag = payload.get("deprecated") if isinstance(payload, dict) else None
        deprecated = str(flag) if flag else None
    return RegistryLookup(
        ecosystem=ecosystem,  # type: ignore[arg-type]
        package=package,
        found=isinstance(version, str),
        latest_version=version if isinstance(version, str) else None,
        deprecated=deprecated,
        source_url=url,
    )
