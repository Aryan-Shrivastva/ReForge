"""MCP tool server/client helpers, registry lookups, and the A2A planner client (no network)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from pydantic import BaseModel

from reforge.backend.app.agents.a2a import (
    A2AClient,
    A2AError,
    A2APlannerExecutor,
    A2AUnavailable,
    extract_reply_text,
)
from reforge.backend.app.agents.analysis import registry_lookups
from reforge.backend.app.agents.executor import build_agent_executors, role_key
from reforge.backend.app.agents.monitoring import AgentMonitor
from reforge.backend.app.core.config import Settings
from reforge.backend.app.core.input_guard import UNTRUSTED_NOTICE
from reforge.backend.app.tools.mcp_client import McpToolError, decode_tool_result, server_environment
from reforge.backend.app.tools.registry import lookup_latest_version


class Plan(BaseModel):
    steps: list[str]


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def json_opener(routes: dict[str, Any], seen: list[Any] | None = None):
    """A urlopen stand-in that answers by URL suffix."""

    def opener(request, timeout: float):
        if seen is not None:
            seen.append(request)
        for suffix, payload in routes.items():
            if request.full_url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(json.dumps(payload).encode())
        raise HTTPError(request.full_url, 404, "not found", {}, None)

    return opener


# ---------------------------------------------------------------- registry tool


def test_pypi_lookup_reads_latest_version_and_inactive_status() -> None:
    opener = json_opener({"/pypi/flask/json": {"info": {"version": "3.1.0", "classifiers": ["Development Status :: 7 - Inactive"]}}})
    result = lookup_latest_version("pypi", "flask", opener=opener)
    assert result.found and result.latest_version == "3.1.0"
    assert result.deprecated == "Development Status: 7 - Inactive"


def test_npm_lookup_handles_scoped_packages_and_deprecation() -> None:
    seen: list[Any] = []
    opener = json_opener({"/@babel/core/latest": {"version": "7.26.0", "deprecated": "use x"}}, seen)
    result = lookup_latest_version("npm", "@babel/core", opener=opener)
    assert result.latest_version == "7.26.0" and result.deprecated == "use x"
    assert seen[0].full_url == "https://registry.npmjs.org/@babel/core/latest"


def test_missing_package_and_invalid_names() -> None:
    assert lookup_latest_version("npm", "no-such-pkg", opener=json_opener({})).found is False
    with pytest.raises(ValueError):
        lookup_latest_version("pypi", "../etc/passwd")
    with pytest.raises(ValueError):
        lookup_latest_version("maven", "junit")


def test_analysis_registry_lookups_pick_the_right_ecosystem() -> None:
    class Client:
        calls: list[tuple[str, dict[str, str]]] = []

        def call_many(self, calls):
            type(self).calls = calls
            return [{"ecosystem": args["ecosystem"], "package": args["package"], "latest_version": "1.0", "found": True} for _, args in calls]

    dependencies = [
        {"name": "express", "source": "package.json:dependencies"},
        {"name": "flask", "source": "requirements.txt"},
        {"name": "Flask", "source": "pyproject.toml"},  # duplicate (case-insensitive)
        {"name": "org.junit:junit", "source": "build.gradle"},
    ]
    results = registry_lookups(dependencies, Client())
    assert [(item["ecosystem"], item["package"]) for item in results] == [("npm", "express"), ("pypi", "flask")]


# ---------------------------------------------------------------- MCP client


def test_decode_tool_result_prefers_structured_content_and_validates() -> None:
    structured = SimpleNamespace(
        isError=False,
        structuredContent={"result": {"ecosystem": "pypi", "package": "x", "found": False, "source_url": "u"}},
        content=[],
    )
    assert decode_tool_result("lookup_latest_version", structured)["package"] == "x"

    text = SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(text='{"matches": []}')])
    assert decode_tool_result("search_code", text) == {"matches": []}


def test_decode_tool_result_raises_on_errors_and_bad_payloads() -> None:
    with pytest.raises(McpToolError, match="failed"):
        decode_tool_result("read_file", SimpleNamespace(isError=True, content=[SimpleNamespace(text="boom")]))
    with pytest.raises(McpToolError, match="invalid result"):
        decode_tool_result("lookup_latest_version", SimpleNamespace(isError=False, structuredContent={"found": "?"}, content=[]))
    with pytest.raises(McpToolError, match="non-JSON"):
        decode_tool_result("read_file", SimpleNamespace(isError=False, structuredContent=None, content=[SimpleNamespace(text="hi")]))


def test_server_environment_points_the_server_at_the_workspace(tmp_path: Path) -> None:
    environment = server_environment(tmp_path)
    assert environment["REFORGE_MCP_WORKSPACE"] == str(tmp_path)
    assert environment["PYTHONPATH"]


def test_mcp_server_tools_are_registered(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("mcp", reason="the mcp package is not installed")
    monkeypatch.setenv("REFORGE_MCP_WORKSPACE", str(tmp_path))
    from reforge.backend.app import mcp_server

    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    assert mcp_server.read_file("app.py")["content"].startswith("print")
    with pytest.raises(Exception):
        mcp_server.read_file("../outside.txt")


# ---------------------------------------------------------------- A2A

CARD = {"name": "reforge_planner", "url": "http://planner:8002/"}


def test_a2a_client_discovers_card_and_sends_message_send() -> None:
    seen: list[Any] = []
    reply = {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", "status": {"state": "completed"},
             "artifacts": [{"parts": [{"kind": "text", "text": '{"steps": ["a"]}'}]}]}}
    opener = json_opener({"/.well-known/agent-card.json": CARD, "planner:8002/": reply}, seen)
    client = A2AClient("http://planner:8002", opener=opener)

    text = client.send_text("plan please", context_id="reforge-run1")

    assert text == '{"steps": ["a"]}'
    body = json.loads(seen[-1].data)
    assert body["method"] == "message/send"
    assert body["params"]["message"]["contextId"] == "reforge-run1"
    assert body["params"]["message"]["parts"][0] == {"kind": "text", "text": "plan please"}


def test_extract_reply_text_handles_messages_history_and_failures() -> None:
    assert extract_reply_text({"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}) == "hi"
    history = {"kind": "task", "status": {"state": "completed"}, "history": [
        {"role": "user", "parts": [{"text": "q"}]}, {"role": "agent", "parts": [{"text": "answer"}]}]}
    assert extract_reply_text(history) == "answer"
    with pytest.raises(A2AError, match="failed"):
        extract_reply_text({"kind": "task", "status": {"state": "failed", "message": {"parts": [{"text": "x"}]}}})


def test_a2a_unreachable_server_raises_unavailable() -> None:
    client = A2AClient("http://planner:8002", opener=json_opener({".json": URLError("connection refused")}))
    with pytest.raises(A2AUnavailable):
        client.send_text("hi")


def test_planner_executor_validates_reply_and_uses_run_context() -> None:
    class Client:
        base_url = "http://planner:8002"
        sent: list[tuple[str, str | None]] = []

        def send_text(self, text: str, *, context_id: str | None = None) -> str:
            self.sent.append((text, context_id))
            return '```json\n{"steps": ["upgrade"]}\n```'

    monitor = AgentMonitor()
    executor = A2APlannerExecutor(Client(), monitor=monitor)  # type: ignore[arg-type]
    executor.set_run_context(run_id="run1", workspace=None)

    plan = executor.run(role="Planner", goal="g", backstory="b", prompt="Plan it", expected_output="Plan JSON.", output_model=Plan)

    assert plan == Plan(steps=["upgrade"])
    text, context = Client.sent[0]
    assert text.startswith(UNTRUSTED_NOTICE) and context == "reforge-run1"
    assert [event.kind for event in monitor.drain()] == ["tool_call", "result"]


def test_planner_executor_falls_back_in_process_when_a2a_is_down() -> None:
    class DownClient:
        base_url = "http://planner:8002"

        def send_text(self, text: str, *, context_id: str | None = None) -> str:
            raise A2AUnavailable("connection refused")

    class Local:
        def run(self, **kwargs: Any) -> Plan:
            return Plan(steps=["local"])

    monitor = AgentMonitor()
    executor = A2APlannerExecutor(DownClient(), monitor=monitor, fallback=Local())  # type: ignore[arg-type]
    plan = executor.run(role="Planner", goal="g", backstory="b", prompt="p", expected_output="e", output_model=Plan)
    assert plan == Plan(steps=["local"])
    assert any("in-process" in event.summary for event in monitor.drain())


def test_build_agent_executors_routes_planning_over_a2a() -> None:
    settings = Settings(agent_framework="hybrid", a2a_planner_url="http://planner:8002")
    fake = SimpleNamespace(framework="crewai", usage=None)
    executors = build_agent_executors(settings, crewai_factory=lambda _: fake, adk_factory=lambda _: fake)

    assert executors.frameworks()["planning"] == "adk-a2a"
    assert executors.planning.fallback is fake
    assert executors.transformation is fake


@pytest.mark.parametrize(
    "role, key",
    [("Repository modernization analyst", "analysis"), ("Modernization planner", "planning"),
     ("Code transformer", "transformation"), ("Test failure repairer", "repair"),
     ("Characterization test generator", "test_generation"), ("Someone else", "agent")],
)
def test_role_key(role: str, key: str) -> None:
    assert role_key(role) == key
