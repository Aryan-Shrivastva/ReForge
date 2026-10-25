"""Agent-runtime selection (CrewAI / Google ADK / hybrid), ADK sessions, and token accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from reforge.backend.app.agents.adk_adapter import ADKComponents, ADKExecutionError, ADKExecutor, agent_identifier
from reforge.backend.app.agents.crewai_adapter import CrewAIComponents, CrewAIExecutor
from reforge.backend.app.agents.executor import AgentExecutors, TokenUsage, build_agent_executors
from reforge.backend.app.core.config import Settings


class Output(BaseModel):
    summary: str
    count: int


# --- runtime selection -------------------------------------------------------


@dataclass
class StubExecutor:
    framework: str
    usage: TokenUsage = field(default_factory=TokenUsage)


def executors_for(framework: str) -> AgentExecutors:
    return build_agent_executors(
        Settings(agent_framework=framework, llm_model="m"),
        crewai_factory=lambda _: StubExecutor("crewai"),  # type: ignore[arg-type,return-value]
        adk_factory=lambda _: StubExecutor("adk"),  # type: ignore[arg-type,return-value]
    )


def test_hybrid_runs_reasoning_agents_on_adk_and_patch_authors_on_crewai() -> None:
    executors = executors_for("hybrid")

    assert executors.frameworks() == {
        "analysis": "adk",
        "planning": "adk",
        "transformation": "crewai",
        "repair": "crewai",
        "test_generation": "crewai",
    }
    # One instance per framework, so sessions and token counts span the run.
    assert executors.analysis is executors.planning
    assert executors.transformation is executors.repair is executors.test_generation
    assert len(executors.unique()) == 2


@pytest.mark.parametrize("framework", ["crewai", "adk"])
def test_single_framework_modes_share_one_executor(framework: str) -> None:
    executors = executors_for(framework)

    assert set(executors.frameworks().values()) == {framework}
    assert len(executors.unique()) == 1


def test_usage_is_summed_across_unique_executors() -> None:
    executors = executors_for("hybrid")
    executors.analysis.usage.add(10, 5)
    executors.repair.usage.add(3, 2)

    assert executors.usage() == TokenUsage(prompt_tokens=13, completion_tokens=7)


def test_unknown_framework_is_rejected() -> None:
    with pytest.raises(ValueError, match="AGENT_FRAMEWORK"):
        Settings(agent_framework="autogen")


# --- CrewAI token accounting ----------------------------------------------------


def test_crewai_executor_records_provider_token_usage() -> None:
    class Crew:
        def __init__(self, **_: object) -> None:
            pass

        def kickoff(self):
            return SimpleNamespace(
                pydantic=Output(summary="ok", count=1),
                token_usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
            )

    components = CrewAIComponents(
        Agent=lambda **kwargs: kwargs,
        Crew=Crew,
        Task=lambda **kwargs: kwargs,
        Process=SimpleNamespace(sequential="sequential"),
        LLM=lambda **kwargs: kwargs,
    )
    executor = CrewAIExecutor(provider="gemini", model="gemini-2.5-flash", component_loader=lambda _: components)

    value = executor.run(role="r", goal="g", backstory="b", prompt="p", expected_output="e", output_model=Output)

    assert value.count == 1
    assert executor.usage == TokenUsage(prompt_tokens=120, completion_tokens=30)
    assert executor._llm_kwargs()["model"] == "gemini/gemini-2.5-flash"


# --- Google ADK with in-process fakes ------------------------------------------


class FakeSession:
    def __init__(self, session_id: str, state: dict[str, Any]) -> None:
        self.id = session_id
        self.state = dict(state)
        self.events: list[Any] = []


class FakeSessionService:
    def __init__(self) -> None:
        self.sessions: dict[str, FakeSession] = {}

    async def create_session(self, *, app_name: str, user_id: str, session_id: str, state: dict[str, Any]):
        self.sessions[session_id] = FakeSession(session_id, state)
        return self.sessions[session_id]

    async def get_session(self, *, app_name: str, user_id: str, session_id: str):
        return self.sessions.get(session_id)

    async def append_event(self, session: FakeSession, event: Any):
        session.events.append(event)
        session.state.update(event.actions.state_delta or {})
        return event


@dataclass
class FakeEventActions:
    state_delta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeEvent:
    invocation_id: str = ""
    author: str = ""
    actions: FakeEventActions = field(default_factory=FakeEventActions)
    timestamp: float = 0.0
    content: Any = None
    usage_metadata: Any = None
    error_message: str | None = None
    final: bool = False

    def is_final_response(self) -> bool:
        return self.final


class FakeRunner:
    agents: list[dict[str, Any]] = []
    reply = '{"summary": "analysed", "count": 2}'

    def __init__(self, *, agent: dict[str, Any], app_name: str, session_service: FakeSessionService) -> None:
        self.agent = agent
        self.service = session_service
        FakeRunner.agents.append(agent)

    async def run_async(self, *, user_id: str, session_id: str, new_message: Any, state_delta: dict[str, Any]):
        session = self.service.sessions[session_id]
        session.state.update(state_delta)
        # Mimic ADK instruction templating from session state.
        instruction = self.agent["instruction"]
        for key, value in session.state.items():
            instruction = instruction.replace("{" + key + "?}", str(value))
        self.agent["rendered_instruction"] = instruction
        self.agent["message"] = new_message.parts[0].text
        session.state[self.agent["output_key"]] = self.reply
        yield FakeEvent(
            content=SimpleNamespace(parts=[SimpleNamespace(text=self.reply)]),
            usage_metadata=SimpleNamespace(prompt_token_count=40, candidates_token_count=9),
            final=True,
        )


def fake_components() -> ADKComponents:
    FakeRunner.agents = []
    return ADKComponents(
        LlmAgent=lambda **kwargs: kwargs,
        Runner=FakeRunner,
        InMemorySessionService=FakeSessionService,
        DatabaseSessionService=None,
        Event=FakeEvent,
        EventActions=FakeEventActions,
        LiteLlm=lambda **kwargs: ("litellm", kwargs),
        types=SimpleNamespace(
            Content=lambda role, parts: SimpleNamespace(role=role, parts=parts),
            Part=lambda text: SimpleNamespace(text=text),
            GenerateContentConfig=lambda **kwargs: kwargs,
        ),
    )


def test_adk_executor_uses_a_run_session_with_state_updates() -> None:
    components = fake_components()
    executor = ADKExecutor(provider="openai", model="gpt-4.1-mini", component_loader=lambda: components)

    executor.bind_run("run-1", {"objective": "Upgrade express", "stage": "ANALYZE", "repository": "repo.zip"})
    value = executor.run(
        role="Repository modernization analyst",
        goal="Produce {an} analysis.",
        backstory="Evidence only.",
        prompt='Evidence: {"files": []}',
        expected_output="One Output JSON object.",
        output_model=Output,
    )
    executor.update_run_state({"stage": "PLAN"})
    state = executor.run_state()

    agent = FakeRunner.agents[0]
    assert value == Output(summary="analysed", count=2)
    assert agent["name"] == "repository_modernization_analyst"
    assert agent["include_contents"] == "none"
    assert agent["output_key"] == "repository_modernization_analyst_output"
    assert agent["model"] == ("litellm", {"model": "openai/gpt-4.1-mini"})
    assert "Upgrade express" in agent["rendered_instruction"]
    assert "{an}" not in agent["instruction"]  # caller braces never reach ADK templating
    assert agent["message"] == 'Evidence: {"files": []}'
    assert executor.usage == TokenUsage(prompt_tokens=40, completion_tokens=9)
    assert state["stage"] == "PLAN"
    assert state["agent_calls"] == 1
    assert json.loads(state["repository_modernization_analyst_validated"])["count"] == 2
    assert "analysed" in state["agent_history"]


def test_adk_executor_rejects_invalid_model_output() -> None:
    components = fake_components()
    FakeRunner.reply = '{"summary": "missing count"}'
    executor = ADKExecutor(provider="gemini", model="gemini-2.5-flash", component_loader=lambda: components)
    try:
        with pytest.raises(ADKExecutionError, match="valid Output"):
            executor.run(role="r", goal="g", backstory="b", prompt="p", expected_output="e", output_model=Output)
    finally:
        FakeRunner.reply = '{"summary": "analysed", "count": 2}'


def test_adk_gemini_models_are_native_and_others_use_litellm() -> None:
    components = fake_components()

    assert ADKExecutor(provider="gemini", model="gemini/gemini-2.5-pro")._model(components) == "gemini-2.5-pro"
    assert ADKExecutor(provider="anthropic", model="claude-sonnet-5-5")._model(components) == (
        "litellm", {"model": "anthropic/claude-sonnet-5-5"}
    )


def test_agent_identifiers_are_valid_python_names() -> None:
    assert agent_identifier("Baseline characterization-test generator") == "baseline_characterization_test_generator"
    assert agent_identifier("3D planner") == "agent_3d_planner"


# --- real Google ADK (runs where google-adk is installed) ----------------------


def test_real_adk_runner_session_and_state_delta() -> None:
    pytest.importorskip("google.adk", reason="google-adk is not installed in this environment")
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    instructions: list[str] = []

    class DeterministicLlm(BaseLlm):
        model: str = "deterministic"

        async def generate_content_async(self, llm_request, stream=False):
            instructions.append(str(llm_request.config.system_instruction))
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text='{"summary": "ok", "count": 3}')]),
                usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, candidates_token_count=2),
            )

    executor = ADKExecutor(provider="openai", model="unused", model_override=DeterministicLlm())
    executor.bind_run("real-run", {"objective": "Replace url.parse", "stage": "PLAN"})
    value = executor.run(
        role="Repository modernization planner",
        goal="Plan.",
        backstory="Small steps.",
        prompt="Plan please.",
        expected_output="One Output JSON object.",
        output_model=Output,
    )
    executor.update_run_state({"stage": "BASELINE"})
    state = executor.run_state()
    executor.close()

    assert value.count == 3
    assert "Replace url.parse" in instructions[0]
    assert state["stage"] == "BASELINE"
    assert state["repository_modernization_planner_output"]
    assert executor.usage == TokenUsage(prompt_tokens=5, completion_tokens=2)
