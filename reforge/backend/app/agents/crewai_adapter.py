"""CrewAI execution adapter for ReForge's structured reasoning agents.

The orchestrator remains plain deterministic Python.  This module is the
boundary where Analysis and Planning become real CrewAI ``Agent``/``Task``/
``Crew`` executions with Pydantic output contracts.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.config import Settings
from ..core.input_guard import harden_prompt
from ..llm import LLMConfigurationError
from ..tools.mcp_client import AGENT_READ_TOOLS, stdio_server_params
from .a2a import A2AClient, consult_planner_tool
from .executor import TokenUsage, role_key
from .monitoring import AgentMonitor, crewai_output_guardrail, crewai_step_callback, crewai_task_callback

ModelT = TypeVar("ModelT", bound=BaseModel)


class CrewAIExecutionError(RuntimeError):
    """Raised when CrewAI cannot return a valid contracted result."""


@dataclass(frozen=True, slots=True)
class CrewAIComponents:
    """Imported CrewAI types, kept injectable to make the adapter testable."""

    Agent: type[Any]
    Crew: type[Any]
    Task: type[Any]
    Process: type[Any]
    LLM: type[Any]


def _runtime_storage_root() -> Path:
    """Choose an ignored, project-local location for CrewAI runtime state."""
    configured = os.getenv("REFORGE_CREWAI_STORAGE")
    return Path(configured) if configured else Path.cwd() / ".reforge-crewai"


def _load_crewai(storage_root: Path | None = None) -> CrewAIComponents:
    """Import CrewAI after placing its Windows-only runtime data under ReForge.

    CrewAI 1.x relies on ``appdirs`` and an OS app-data folder during import.
    Patching that dependency before importing CrewAI makes the location explicit,
    testable, and independent of the developer's global user profile.
    """
    root = (storage_root or _runtime_storage_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    temporary_root = root / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
    os.environ["LOCALAPPDATA"] = str(root)
    # CrewAI creates a temporary settings file during import. Keep it out of
    # Windows' user-temp directory, which may be inaccessible to a sandboxed
    # process, and alongside the other ignored CrewAI runtime data instead.
    os.environ["TEMP"] = str(temporary_root)
    os.environ["TMP"] = str(temporary_root)
    tempfile.tempdir = str(temporary_root)

    try:
        from crewai_core import settings as crewai_settings
    except ImportError:  # pragma: no cover - older/newer CrewAI layouts.
        crewai_settings = None
    if crewai_settings is not None:
        # CrewAI otherwise probes the user's profile directory before its
        # project temp fallback. Point that probe at the local runtime root.
        crewai_settings.DEFAULT_CONFIG_PATH = root / "config" / "settings.json"

    try:
        import appdirs
    except ImportError:
        # appdirs is only needed to redirect CrewAI's Windows app-data folder;
        # CrewAI versions that no longer use it work without the patch.
        appdirs = None
    if appdirs is not None:
        _patch_appdirs(appdirs, root)
    try:
        from crewai import Agent, Crew, LLM, Process, Task
    except ImportError as exc:  # pragma: no cover - installation failure is environment-specific.
        raise CrewAIExecutionError(
            "CrewAI is unavailable. Install the project dependencies before starting a run."
        ) from exc
    return CrewAIComponents(Agent=Agent, Crew=Crew, Task=Task, Process=Process, LLM=LLM)


def _patch_appdirs(appdirs: Any, root: Path) -> None:
    original_user_data_dir = appdirs.user_data_dir

    def reforge_user_data_dir(
        appname: str | None = None,
        appauthor: str | None = None,
        version: str | None = None,
        roaming: bool = False,
    ) -> str:
        if appauthor == "CrewAI":
            path = root / appauthor
            if appname:
                path /= appname
            if version:
                path /= version
            return str(path)
        return original_user_data_dir(appname, appauthor, version, roaming)

    appdirs.user_data_dir = reforge_user_data_dir


@dataclass(slots=True)
class CrewAIExecutor:
    """Execute one bounded, sequential CrewAI task with a typed result."""

    framework: ClassVar[str] = "crewai"

    provider: str
    model: str
    temperature: float = 0.1
    storage_root: Path | None = None
    component_loader: Callable[[Path | None], CrewAIComponents] = _load_crewai
    usage: TokenUsage = field(default_factory=TokenUsage)
    # Monitoring/guardrail callbacks, MCP tools, and the A2A planner tool.
    monitor: AgentMonitor | None = None
    enable_mcp: bool = False
    a2a_planner_url: str | None = None
    _workspace: Path | None = field(default=None, init=False, repr=False)

    def set_run_context(self, *, run_id: str, workspace: Path) -> None:
        """Bind the run's workspace so MCP tools are jailed to it."""
        self._workspace = Path(workspace)

    def __post_init__(self) -> None:
        if self.provider not in {"openai", "anthropic", "ollama", "gemini"}:
            raise ValueError("provider must be openai, anthropic, ollama, or gemini")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

    def _model_name(self) -> str:
        """Use CrewAI's native-provider spelling where supported."""
        if self.provider == "openai":
            return self.model.removeprefix("openai/")
        return f"{self.provider}/{self.model.removeprefix(f'{self.provider}/')}"

    def _llm_kwargs(self) -> dict[str, Any]:
        model_name = self._model_name()
        kwargs: dict[str, Any] = {"model": model_name}
        # GPT-5 models currently accept only their server-side default
        # temperature. Supplying ReForge's generic 0.1 default makes the
        # provider reject an otherwise valid request.
        if not (self.provider == "openai" and model_name.startswith("gpt-5")):
            kwargs["temperature"] = self.temperature
        if self.provider == "openai" and os.getenv("OPENAI_BASE_URL"):
            kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
        if self.provider == "ollama" and os.getenv("OLLAMA_BASE_URL"):
            kwargs["base_url"] = os.environ["OLLAMA_BASE_URL"]
        return kwargs

    def run(
        self,
        *,
        role: str,
        goal: str,
        backstory: str,
        prompt: str,
        expected_output: str,
        output_model: type[ModelT],
        validator: Callable[[ModelT], None] | None = None,
    ) -> ModelT:
        """Run a single non-delegating agent and enforce the output schema."""
        components = self.component_loader(self.storage_root)
        key = role_key(role)
        hardened, findings = harden_prompt(prompt)
        if self.monitor is not None:
            for finding in findings:
                self.monitor.record(key, "decision", f"Possible prompt injection in quoted content ({finding.rule}); treated as data", excerpt=finding.excerpt)
        with ExitStack() as resources:
            try:
                tools = self._tools_for(key, resources)
                llm = components.LLM(**self._llm_kwargs())
                agent = components.Agent(
                    role=role,
                    goal=goal,
                    backstory=backstory,
                    llm=llm,
                    tools=tools,
                    allow_delegation=False,
                    verbose=False,
                    memory=False,
                    step_callback=crewai_step_callback(self.monitor, key),
                )
                task = components.Task(
                    description=hardened,
                    expected_output=expected_output,
                    agent=agent,
                    output_pydantic=output_model,
                    callback=crewai_task_callback(self.monitor, key),
                    guardrail=crewai_output_guardrail(self.monitor, key, output_model),
                )
                crew = components.Crew(
                    agents=[agent],
                    tasks=[task],
                    process=components.Process.sequential,
                    verbose=False,
                    memory=False,
                    share_crew=False,
                )
                output = crew.kickoff()
            except Exception as exc:
                if self.monitor is not None:
                    self.monitor.record(key, "error", f"CrewAI execution failed: {type(exc).__name__}: {str(exc)[:200]}")
                raise CrewAIExecutionError(f"CrewAI execution failed: {exc}") from exc
        try:
            token_usage = getattr(output, "token_usage", None)
            if token_usage is not None:
                self.usage.add(
                    getattr(token_usage, "prompt_tokens", 0),
                    getattr(token_usage, "completion_tokens", 0),
                )
            structured = getattr(output, "pydantic", None)
            value = output_model.model_validate(
                structured.model_dump() if isinstance(structured, BaseModel) else structured
            )
            if validator is not None:
                validator(value)
            return value
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            raise CrewAIExecutionError(
                f"CrewAI did not produce a valid {output_model.__name__}: {exc}"
            ) from exc
        except Exception as exc:
            raise CrewAIExecutionError(f"CrewAI execution failed: {exc}") from exc

    def _tools_for(self, key: str, resources: ExitStack) -> list[Any]:
        """MCP read tools for the patch authors; the A2A planner tool for the repair agent.

        Tool start-up failures are recorded and the agent runs without that
        tool: tools improve an answer but are never required for one.
        """
        tools: list[Any] = []
        if self.enable_mcp and self._workspace is not None and key in {"transformation", "repair"}:
            try:
                from crewai_tools import MCPServerAdapter

                adapter_tools = resources.enter_context(MCPServerAdapter(stdio_server_params(self._workspace)))
                tools.extend(tool for tool in adapter_tools if getattr(tool, "name", "") in AGENT_READ_TOOLS)
                if self.monitor is not None:
                    self.monitor.record(key, "tool_call", f"Connected MCP tools: {', '.join(sorted(getattr(tool, 'name', '?') for tool in tools))}")
            except Exception as exc:  # missing package, server start failure, protocol error
                if self.monitor is not None:
                    self.monitor.record(key, "error", f"MCP tools unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        if self.a2a_planner_url and key == "repair":
            try:
                tools.append(consult_planner_tool(A2AClient(self.a2a_planner_url), self.monitor))
            except Exception as exc:
                if self.monitor is not None:
                    self.monitor.record(key, "error", f"A2A planner tool unavailable: {type(exc).__name__}: {str(exc)[:200]}")
        return tools


def build_crewai_executor(settings: Settings, monitor: AgentMonitor | None = None) -> CrewAIExecutor:
    """Build the actual agent runtime from standard ReForge LLM settings."""
    if not settings.llm_model:
        raise LLMConfigurationError("LLM_MODEL must be configured before starting a CrewAI run")
    return CrewAIExecutor(
        provider=settings.llm_provider,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        monitor=monitor,
        enable_mcp=settings.enable_mcp,
        a2a_planner_url=settings.a2a_planner_url,
    )
