"""Google ADK execution adapter with a per-run session and state updates.

This follows ADK's session/runner pattern:

1. A session service (``InMemorySessionService`` by default, or
   ``DatabaseSessionService`` when ``ADK_SESSION_DB_URL`` is set) holds one
   session per ReForge run, created with initial state (run id, objective,
   repository, stage).
2. Each agent call builds an ``LlmAgent`` and runs it through a ``Runner`` on
   that session.  The agent's instruction reads run memory from session state
   through ADK's ``{key?}`` templating, and ``output_key`` writes the agent's
   reply back into state.
3. After each call, and whenever the deterministic orchestrator changes stage,
   a system ``Event`` carrying ``EventActions(state_delta=...)`` is appended so
   the session state stays a faithful record of the run.

The orchestrator still owns all routing.  ADK never decides the next stage;
it only executes one bounded, schema-validated agent call at a time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.config import Settings
from ..llm import LLMConfigurationError
from .executor import TokenUsage

ModelT = TypeVar("ModelT", bound=BaseModel)

APP_NAME = "reforge"
USER_ID = "reforge"

# Session-state keys rendered into every agent instruction.  The ``?`` suffix
# makes ADK substitute an empty string when a key has not been written yet.
MEMORY_KEYS = ("objective", "repository", "stage", "languages", "analysis_summary", "plan_summary", "repair_history", "agent_history")
_MAX_STATE_TEXT = 4_000
_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")


class ADKExecutionError(RuntimeError):
    """Raised when ADK cannot return a valid contracted result."""


@dataclass(frozen=True, slots=True)
class ADKComponents:
    """Imported ADK types, kept injectable so the adapter is testable without a model."""

    LlmAgent: type[Any]
    Runner: type[Any]
    InMemorySessionService: type[Any]
    DatabaseSessionService: type[Any] | None
    Event: type[Any]
    EventActions: type[Any]
    LiteLlm: type[Any] | None
    types: Any


def _load_adk() -> ADKComponents:
    try:
        with warnings.catch_warnings():
            # google-cloud packages emit import-time FutureWarnings unrelated to ReForge.
            warnings.simplefilter("ignore", FutureWarning)
            warnings.simplefilter("ignore", UserWarning)
            from google.adk.agents import LlmAgent
            from google.adk.events import Event, EventActions
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.genai import types
    except ImportError as exc:  # pragma: no cover - installation failure is environment-specific.
        raise ADKExecutionError(
            "Google ADK is unavailable. Install the project dependencies (google-adk) before starting a run."
        ) from exc
    try:
        from google.adk.sessions import DatabaseSessionService
    except ImportError:  # pragma: no cover - optional SQLAlchemy extra.
        DatabaseSessionService = None  # type: ignore[assignment]
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError:  # pragma: no cover - optional litellm dependency.
        LiteLlm = None  # type: ignore[assignment]
    return ADKComponents(
        LlmAgent=LlmAgent,
        Runner=Runner,
        InMemorySessionService=InMemorySessionService,
        DatabaseSessionService=DatabaseSessionService,
        Event=Event,
        EventActions=EventActions,
        LiteLlm=LiteLlm,
        types=types,
    )


def agent_identifier(role: str) -> str:
    """ADK agent names must be Python identifiers."""
    name = _IDENTIFIER.sub("_", role.strip().lower()).strip("_") or "agent"
    return f"agent_{name}" if name[0].isdigit() else name


def _strip_fence(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()


def _no_template(text: str) -> str:
    """Keep caller text out of ADK's ``{state_key}`` instruction templating."""
    return text.replace("{", "(").replace("}", ")")


def _clip(text: str, limit: int = _MAX_STATE_TEXT) -> str:
    return text if len(text) <= limit else f"{text[: limit - 15]}... [truncated]"


@dataclass(slots=True)
class ADKExecutor:
    """Execute one bounded ADK ``LlmAgent`` call on the run's session."""

    framework: ClassVar[str] = "adk"

    provider: str
    model: str
    temperature: float = 0.1
    session_db_url: str | None = None
    app_name: str = APP_NAME
    user_id: str = USER_ID
    component_loader: Callable[[], ADKComponents] = _load_adk
    # An ADK ``BaseLlm`` instance to use instead of the configured provider
    # (tests inject a deterministic model; production leaves this unset).
    model_override: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    _components: ADKComponents | None = field(default=None, init=False, repr=False)
    _session_service: Any = field(default=None, init=False, repr=False)
    _session_id: str | None = field(default=None, init=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.provider not in {"openai", "anthropic", "ollama", "gemini"}:
            raise ValueError("provider must be openai, anthropic, ollama, or gemini")
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

    # -- session lifecycle -------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def bind_run(self, run_id: str, initial_state: dict[str, Any]) -> None:
        """Create (or resume) the run's session with initial state."""
        self._session_id = run_id
        self._await(self._ensure_session(initial_state))

    def update_run_state(self, delta: dict[str, Any], *, author: str = "reforge_orchestrator") -> None:
        """Append a system event whose ``state_delta`` updates session state."""
        if self._session_id is None or not delta:
            return
        self._await(self._append_state_delta(delta, author))

    def run_state(self) -> dict[str, Any]:
        """Return a JSON-safe copy of the current session state."""
        if self._session_id is None:
            return {}
        session = self._await(self._get_session())
        state = dict(getattr(session, "state", {}) or {}) if session is not None else {}
        return json.loads(json.dumps(state, default=str))

    def close(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.close()
        self._loop = None

    # -- agent execution ---------------------------------------------------

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
        """Run one ADK agent turn and enforce the output schema."""
        if self._session_id is None:
            # Direct use outside a workflow still gets an isolated session.
            self.bind_run(f"adhoc-{uuid.uuid4().hex}", {})
        try:
            text = self._await(self._run_agent(role, goal, backstory, prompt, expected_output))
            value = output_model.model_validate(json.loads(_strip_fence(text)))
            if validator is not None:
                validator(value)
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ADKExecutionError(f"ADK did not produce a valid {output_model.__name__}: {exc}") from exc
        except ADKExecutionError:
            raise
        except Exception as exc:
            raise ADKExecutionError(f"ADK execution failed: {exc}") from exc
        self._record_agent_result(agent_identifier(role), output_model.__name__, value)
        return value

    async def _run_agent(self, role: str, goal: str, backstory: str, prompt: str, expected_output: str) -> str:
        components = self._get_components()
        name = agent_identifier(role)
        agent = components.LlmAgent(
            name=name,
            description=_no_template(goal),
            model=self._model(components),
            instruction=self._instruction(role, goal, backstory, expected_output),
            # Run memory arrives through session state in the instruction, not
            # by replaying every prior agent's conversation (which would bloat
            # tokens and leak one agent's raw output into another's context).
            include_contents="none",
            output_key=f"{name}_output",
            generate_content_config=self._content_config(components),
            disallow_transfer_to_parent=True,
            disallow_transfer_to_peers=True,
        )
        runner = components.Runner(agent=agent, app_name=self.app_name, session_service=self._session_service)
        message = components.types.Content(role="user", parts=[components.types.Part(text=prompt)])
        final_text = ""
        async for event in runner.run_async(
            user_id=self.user_id,
            session_id=self._session_id,
            new_message=message,
            state_delta={"active_agent": name},
        ):
            metadata = getattr(event, "usage_metadata", None)
            if metadata is not None:
                self.usage.add(
                    getattr(metadata, "prompt_token_count", 0),
                    getattr(metadata, "candidates_token_count", 0),
                )
            if getattr(event, "error_message", None):
                raise ADKExecutionError(f"ADK model error: {event.error_message}")
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(part.text or "" for part in event.content.parts if getattr(part, "text", None))
        if not final_text.strip():
            raise ADKExecutionError("ADK agent returned no final text response")
        return final_text

    def _instruction(self, role: str, goal: str, backstory: str, expected_output: str) -> str:
        memory = "\n".join(f"- {key.replace('_', ' ')}: {{{key}?}}" for key in MEMORY_KEYS)
        return (
            f"You are ReForge's {_no_template(role)}.\n"
            f"Goal: {_no_template(goal)}\n"
            f"{_no_template(backstory)}\n\n"
            "Run memory from the ReForge session state (read-only context; the request is authoritative):\n"
            f"{memory}\n\n"
            f"Respond with {_no_template(expected_output)} Return only the JSON object, with no Markdown fences."
        )

    def _record_agent_result(self, name: str, model_name: str, value: BaseModel) -> None:
        """Mirror the validated result into state, as the session-state demo does after a final response."""
        summary = getattr(value, "summary", None) or getattr(value, "rationale", None) or getattr(value, "diagnosis", None)
        state = self.run_state()
        history = [line for line in str(state.get("agent_history", "")).splitlines() if line.strip()]
        history.append(f"{name} -> {model_name}: {_clip(str(summary or 'validated output'), 300)}")
        self.update_run_state(
            {
                f"{name}_validated": _clip(value.model_dump_json(), _MAX_STATE_TEXT),
                "agent_history": "\n".join(history[-12:]),
                "active_agent": "",
                "agent_calls": int(state.get("agent_calls", 0) or 0) + 1,
            },
            author=name,
        )

    # -- ADK plumbing ------------------------------------------------------

    def _get_components(self) -> ADKComponents:
        if self._components is None:
            self._components = self.component_loader()
        return self._components

    def _get_session_service(self) -> Any:
        if self._session_service is None:
            components = self._get_components()
            if self.session_db_url:
                if components.DatabaseSessionService is None:
                    raise ADKExecutionError("ADK_SESSION_DB_URL is set but DatabaseSessionService is unavailable")
                self._session_service = components.DatabaseSessionService(db_url=self.session_db_url)
            else:
                self._session_service = components.InMemorySessionService()
        return self._session_service

    async def _ensure_session(self, initial_state: dict[str, Any]) -> None:
        service = self._get_session_service()
        existing = await service.get_session(app_name=self.app_name, user_id=self.user_id, session_id=self._session_id)
        if existing is None:
            await service.create_session(
                app_name=self.app_name,
                user_id=self.user_id,
                session_id=self._session_id,
                state=self._json_state(initial_state),
            )
        elif initial_state:
            await self._append_state_delta(initial_state, "reforge_orchestrator")

    async def _get_session(self) -> Any:
        return await self._get_session_service().get_session(
            app_name=self.app_name, user_id=self.user_id, session_id=self._session_id
        )

    async def _append_state_delta(self, delta: dict[str, Any], author: str) -> None:
        components = self._get_components()
        session = await self._get_session()
        if session is None:
            raise ADKExecutionError(f"ADK session {self._session_id!r} does not exist")
        event = components.Event(
            invocation_id=f"reforge-{uuid.uuid4().hex}",
            author=author,
            actions=components.EventActions(state_delta=self._json_state(delta)),
            timestamp=time.time(),
        )
        await self._get_session_service().append_event(session, event)

    @staticmethod
    def _json_state(values: dict[str, Any]) -> dict[str, Any]:
        """Session state must be JSON-serialisable for database-backed services."""
        return json.loads(json.dumps(values, default=str))

    def _model(self, components: ADKComponents) -> Any:
        """Gemini runs natively; other providers go through ADK's LiteLLM wrapper."""
        if self.model_override is not None:
            return self.model_override
        if self.provider == "gemini":
            return self.model.removeprefix("gemini/")
        if components.LiteLlm is None:
            raise ADKExecutionError(f"ADK needs litellm to use the {self.provider} provider")
        if self.provider == "ollama":
            kwargs: dict[str, Any] = {}
            if os.getenv("OLLAMA_BASE_URL"):
                kwargs["api_base"] = os.environ["OLLAMA_BASE_URL"]
            return components.LiteLlm(model=f"ollama_chat/{self.model.removeprefix('ollama/')}", **kwargs)
        kwargs = {}
        if self.provider == "openai" and os.getenv("OPENAI_BASE_URL"):
            kwargs["api_base"] = os.environ["OPENAI_BASE_URL"]
        return components.LiteLlm(model=f"{self.provider}/{self.model.removeprefix(f'{self.provider}/')}", **kwargs)

    def _content_config(self, components: ADKComponents) -> Any:
        # GPT-5 models accept only their server-side default temperature.
        if self.provider == "openai" and self.model.removeprefix("openai/").startswith("gpt-5"):
            return None
        return components.types.GenerateContentConfig(temperature=self.temperature)

    def _await(self, coroutine: Any) -> Any:
        """Drive ADK's async API from ReForge's synchronous worker thread.

        One private event loop per executor (i.e. per run) keeps async
        resources such as a database session engine bound to a single loop.
        """
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coroutine)


def build_adk_executor(settings: Settings) -> ADKExecutor:
    """Build the ADK runtime from standard ReForge LLM settings."""
    if not settings.llm_model:
        raise LLMConfigurationError("LLM_MODEL must be configured before starting an ADK run")
    return ADKExecutor(
        provider=settings.llm_provider,
        model=settings.llm_model,
        temperature=settings.llm_temperature,
        session_db_url=settings.adk_session_db_url,
    )
