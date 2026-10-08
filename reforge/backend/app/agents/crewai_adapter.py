"""CrewAI execution adapter for ReForge's structured reasoning agents.

The orchestrator remains plain deterministic Python.  This module is the
boundary where Analysis and Planning become real CrewAI ``Agent``/``Task``/
``Crew`` executions with Pydantic output contracts.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.config import Settings
from ..llm import LLMConfigurationError

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

    import appdirs
    from crewai_core import settings as crewai_settings

    # CrewAI otherwise probes the user's profile directory before its project
    # temp fallback. Point that probe at the same local runtime root first.
    crewai_settings.DEFAULT_CONFIG_PATH = root / "config" / "settings.json"

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
    try:
        from crewai import Agent, Crew, LLM, Process, Task
    except ImportError as exc:  # pragma: no cover - installation failure is environment-specific.
        raise CrewAIExecutionError(
            "CrewAI is unavailable. Install the project dependencies before starting a run."
        ) from exc
    return CrewAIComponents(Agent=Agent, Crew=Crew, Task=Task, Process=Process, LLM=LLM)


@dataclass(slots=True)
class CrewAIExecutor:
    """Execute one bounded, sequential CrewAI task with a typed result."""

    provider: str
    model: str
    temperature: float = 0.1
    storage_root: Path | None = None
    component_loader: Callable[[Path | None], CrewAIComponents] = _load_crewai

    def __post_init__(self) -> None:
        if self.provider not in {"openai", "anthropic", "ollama"}:
            raise ValueError("provider must be openai, anthropic, or ollama")
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
        try:
            llm = components.LLM(**self._llm_kwargs())
            agent = components.Agent(
                role=role,
                goal=goal,
                backstory=backstory,
                llm=llm,
                allow_delegation=False,
                verbose=False,
                memory=False,
            )
            task = components.Task(
                description=prompt,
                expected_output=expected_output,
                agent=agent,
                output_pydantic=output_model,
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


def build_crewai_executor(settings: Settings) -> CrewAIExecutor:
    """Build the actual agent runtime from standard ReForge LLM settings."""
    if not settings.llm_model:
        raise LLMConfigurationError("LLM_MODEL must be configured before starting a CrewAI run")
    return CrewAIExecutor(
        provider=settings.llm_provider,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
    )
