"""Agent-runtime abstraction shared by the CrewAI and Google ADK adapters.

Every reasoning agent (analysis, planning, transformation, repair, test
generation) talks to an :class:`AgentExecutor`: one bounded call that returns a
validated Pydantic model.  The orchestrator stays deterministic Python; only
the runtime that executes an individual agent call is swappable.

``AGENT_FRAMEWORK`` selects the runtime:

* ``crewai`` - every agent runs as a single-task CrewAI crew.
* ``adk``    - every agent runs as a Google ADK ``LlmAgent`` inside a per-run
  ADK session whose state carries run memory.
* ``hybrid`` - analysis and planning (which benefit from session memory across
  the analyse -> plan -> replan cycle) run on ADK; transformation, repair, and
  test generation (role-scoped patch authors) run on CrewAI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from ..core.config import Settings

ModelT = TypeVar("ModelT", bound=BaseModel)

AGENT_ROLES = ("analysis", "planning", "transformation", "repair", "test_generation")
_ADK_ROLES_IN_HYBRID = frozenset({"analysis", "planning"})


@dataclass(slots=True)
class TokenUsage:
    """Cumulative provider-reported token counts for one executor."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        self.prompt_tokens += max(int(prompt_tokens or 0), 0)
        self.completion_tokens += max(int(completion_tokens or 0), 0)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentExecutor(Protocol):
    """One bounded, structured agent call."""

    framework: str
    usage: TokenUsage

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
    ) -> ModelT: ...


@runtime_checkable
class SessionAwareExecutor(Protocol):
    """An executor that keeps per-run session state (Google ADK)."""

    def bind_run(self, run_id: str, initial_state: dict[str, Any]) -> None: ...

    def update_run_state(self, delta: dict[str, Any], *, author: str = "reforge_orchestrator") -> None: ...

    def run_state(self) -> dict[str, Any]: ...


@dataclass(slots=True)
class AgentExecutors:
    """The executor assigned to each agent role for one run."""

    analysis: AgentExecutor
    planning: AgentExecutor
    transformation: AgentExecutor
    repair: AgentExecutor
    test_generation: AgentExecutor

    def for_role(self, role: str) -> AgentExecutor:
        if role not in AGENT_ROLES:
            raise KeyError(role)
        return getattr(self, role)

    def unique(self) -> list[AgentExecutor]:
        seen: dict[int, AgentExecutor] = {}
        for role in AGENT_ROLES:
            executor = self.for_role(role)
            seen.setdefault(id(executor), executor)
        return list(seen.values())

    def frameworks(self) -> dict[str, str]:
        return {role: getattr(self.for_role(role), "framework", "unknown") for role in AGENT_ROLES}

    def usage(self) -> TokenUsage:
        total = TokenUsage()
        for executor in self.unique():
            usage = getattr(executor, "usage", None)
            if isinstance(usage, TokenUsage):
                total.add(usage.prompt_tokens, usage.completion_tokens)
        return total

    def session_executors(self) -> list[SessionAwareExecutor]:
        return [executor for executor in self.unique() if isinstance(executor, SessionAwareExecutor)]


def build_agent_executors(
    settings: Settings,
    *,
    crewai_factory: Callable[[Settings], AgentExecutor] | None = None,
    adk_factory: Callable[[Settings], AgentExecutor] | None = None,
) -> AgentExecutors:
    """Assign an executor to each role according to ``settings.agent_framework``.

    One executor instance is shared by all roles that use the same framework,
    so an ADK session (and token accounting) spans the whole run.
    """
    from .adk_adapter import build_adk_executor
    from .crewai_adapter import build_crewai_executor

    framework = settings.agent_framework
    crew: AgentExecutor | None = None
    adk: AgentExecutor | None = None

    def crew_executor() -> AgentExecutor:
        nonlocal crew
        if crew is None:
            crew = (crewai_factory or build_crewai_executor)(settings)
        return crew

    def adk_executor() -> AgentExecutor:
        nonlocal adk
        if adk is None:
            adk = (adk_factory or build_adk_executor)(settings)
        return adk

    def choose(role: str) -> AgentExecutor:
        if framework == "crewai":
            return crew_executor()
        if framework == "adk":
            return adk_executor()
        return adk_executor() if role in _ADK_ROLES_IN_HYBRID else crew_executor()

    return AgentExecutors(**{role: choose(role) for role in AGENT_ROLES})
