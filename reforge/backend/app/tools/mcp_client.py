"""Client side of ReForge's MCP tool server.

``McpToolClient`` lets deterministic workflow code call MCP tools (the
analysis step uses it for registry lookups), and the ``*_server_params``
helpers give ADK and CrewAI the same launch command for their own MCP
integrations.  The ``mcp`` package is imported lazily so the rest of ReForge
imports without it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..core import tracing
from .registry import RegistryLookup

SERVER_MODULE = "reforge.backend.app.mcp_server"
AGENT_READ_TOOLS = ("read_file", "search_code")
AGENT_ANALYSIS_TOOLS = ("lookup_latest_version", "parse_dependencies", "scan_deprecated_patterns")

# Declared return types: every result is validated before workflow code uses it.
RESULT_MODELS: dict[str, type[BaseModel]] = {"lookup_latest_version": RegistryLookup}


class McpToolError(RuntimeError):
    """Raised when an MCP tool call fails or returns an invalid result."""


def server_environment(workspace: Path | None) -> dict[str, str]:
    environment = dict(os.environ)
    if workspace is not None:
        environment["REFORGE_MCP_WORKSPACE"] = str(workspace.resolve())
    # The server must import ReForge from the same checkout as the caller.
    root = str(Path(__file__).resolve().parents[4])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [root, environment.get("PYTHONPATH", "")]))
    return environment


def stdio_server_params(workspace: Path | None) -> Any:
    """``mcp.StdioServerParameters`` that launch ReForge's MCP server for ``workspace``."""
    from mcp import StdioServerParameters

    return StdioServerParameters(command=sys.executable, args=["-m", SERVER_MODULE], env=server_environment(workspace))


@dataclass(slots=True)
class McpToolClient:
    """Synchronous MCP client for workflow code (one stdio session per batch of calls)."""

    workspace: Path
    timeout_s: float = 30.0

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call_many([(tool, arguments or {})])[0]

    def call_many(self, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        """Run several tool calls over one server session; results are type-checked."""
        if not calls:
            return []
        with tracing.span("mcp.session", mcp__server="reforge-tools", mcp__calls=len(calls)):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(asyncio.wait_for(self._call_many(calls), self.timeout_s * len(calls)))
            except asyncio.TimeoutError as exc:
                raise McpToolError("MCP tool calls timed out") from exc
            finally:
                loop.close()

    async def _call_many(self, calls: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise McpToolError("the 'mcp' package is not installed") from exc
        results: list[dict[str, Any]] = []
        async with stdio_client(stdio_server_params(self.workspace)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for tool, arguments in calls:
                    with tracing.span(f"mcp.tool.{tool}", mcp__tool=tool, mcp__arguments=json.dumps(arguments)[:500]):
                        result = await session.call_tool(tool, arguments)
                        results.append(decode_tool_result(tool, result))
        return results


def decode_tool_result(tool: str, result: Any) -> dict[str, Any]:
    """Turn an MCP ``CallToolResult`` into a validated dict (or raise ``McpToolError``)."""
    if getattr(result, "isError", False):
        raise McpToolError(f"{tool} failed: {_text_of(result)[:300]}")
    payload = getattr(result, "structuredContent", None)
    if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], dict):
        payload = payload["result"]  # FastMCP wraps non-object return annotations as {"result": ...}
    if not isinstance(payload, dict):
        try:
            payload = json.loads(_text_of(result))
        except json.JSONDecodeError as exc:
            raise McpToolError(f"{tool} returned non-JSON content") from exc
    if not isinstance(payload, dict):
        raise McpToolError(f"{tool} returned {type(payload).__name__}, expected an object")
    model = RESULT_MODELS.get(tool)
    if model is not None:
        try:
            payload = model.model_validate(payload).model_dump(mode="json")
        except ValidationError as exc:
            raise McpToolError(f"{tool} returned an invalid result: {exc}") from exc
    return payload


def _text_of(result: Any) -> str:
    return "".join(getattr(item, "text", "") or "" for item in getattr(result, "content", None) or [])
