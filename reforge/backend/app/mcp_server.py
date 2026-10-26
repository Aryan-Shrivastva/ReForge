"""ReForge's MCP tool server (Model Context Protocol, stdio transport).

Agents in both frameworks reach ReForge's tools through MCP:

* Google ADK agents via ``McpToolset`` (analysis: registry lookups, dependency
  parsing, deprecation scanning; planning: read-only code access);
* CrewAI agents via ``crewai_tools.MCPServerAdapter`` (transformation and
  repair: read-only code access);
* ReForge's own analysis step via ``tools.mcp_client.McpToolClient``.

Tools:

========================  ==============================================  ===========
tool                      what it does                                    kind
========================  ==============================================  ===========
lookup_latest_version     latest version + deprecation from PyPI / npm    web API
parse_dependencies        declared dependencies from the manifests        file parser
scan_deprecated_patterns  catalogued deprecated APIs (Python AST, JS)     analyzer
read_file                 one file's text, workspace-jailed               file access
search_code               literal text search, workspace-jailed           file access
========================  ==============================================  ===========

The workspace comes from ``REFORGE_MCP_WORKSPACE`` (set by whoever launches the
server); every path is resolved inside it.  The server never executes
repository code.  Run standalone with ``python -m reforge.backend.app.mcp_server``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .tools.ast_tools import RuleCatalog, scan_all_deprecated_patterns
from .tools.deps_tools import DependencyParser
from .tools.registry import lookup_latest_version as _lookup
from .tools.repo_tools import RepositoryTools

SERVER_NAME = "reforge-tools"
RULES = Path(__file__).resolve().parent / "rules" / "python_deprecations.yaml"
MAX_FILE_CHARS = 40_000
MAX_SEARCH_RESULTS = 200

mcp = FastMCP(SERVER_NAME)


def _workspace() -> RepositoryTools:
    configured = os.getenv("REFORGE_MCP_WORKSPACE", "").strip()
    if not configured:
        raise ValueError("REFORGE_MCP_WORKSPACE is not set; this tool needs a run workspace")
    return RepositoryTools(Path(configured))


@mcp.tool()
def lookup_latest_version(ecosystem: str, package: str) -> dict[str, Any]:
    """Look up the latest published version of a package and whether it is deprecated.

    ecosystem: "pypi" for Python packages or "npm" for JavaScript packages.
    package: the package name exactly as declared (e.g. "flask", "@babel/core").
    """
    return _lookup(ecosystem, package).model_dump(mode="json")


@mcp.tool()
def parse_dependencies() -> dict[str, Any]:
    """List the dependencies declared in the workspace (requirements, pyproject, setup.py, package.json, Gradle)."""
    workspace = _workspace()
    found = DependencyParser(workspace.workspace).parse()
    return {"dependencies": [{"name": item.name, "constraint": item.constraint, "source": item.source} for item in found]}


@mcp.tool()
def scan_deprecated_patterns() -> dict[str, Any]:
    """Find catalogued deprecated API usages (Python AST rules, JavaScript/TypeScript and Android text rules)."""
    workspace = _workspace()
    findings = scan_all_deprecated_patterns(workspace.workspace, RuleCatalog.from_yaml(RULES))
    return {"patterns": [item.model_dump(mode="json") for item in findings]}


@mcp.tool()
def read_file(path: str) -> dict[str, Any]:
    """Read one UTF-8 text file from the workspace (relative path; at most 40,000 characters)."""
    workspace = _workspace()
    text = workspace.read_file(path)
    return {"path": path, "content": text[:MAX_FILE_CHARS], "truncated": len(text) > MAX_FILE_CHARS}


@mcp.tool()
def search_code(text: str, glob: str = "*") -> dict[str, Any]:
    """Find lines containing ``text`` (a literal string, not a regex) in files matching ``glob``.

    Returns at most 200 matches as "path:line:content".
    """
    import re

    if not text or len(text) > 200:
        raise ValueError("search text must be 1 to 200 characters")
    workspace = _workspace()
    matches = workspace.search_code(re.escape(text), glob=glob)
    return {"matches": matches[:MAX_SEARCH_RESULTS], "truncated": len(matches) > MAX_SEARCH_RESULTS}


if __name__ == "__main__":
    mcp.run()  # stdio transport
