from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest
from pydantic import BaseModel

from reforge.backend.app.agents.crewai_adapter import (
    CrewAIComponents,
    CrewAIExecutor,
    _load_crewai,
)


class Result(BaseModel):
    value: str


class FakeLLM:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeAgent:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeTask:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeProcess:
    sequential = "sequential"


class FakeCrew:
    created: dict[str, object] | None = None

    def __init__(self, **kwargs: object) -> None:
        type(self).created = kwargs

    def kickoff(self) -> SimpleNamespace:
        return SimpleNamespace(pydantic=Result(value="crew result"))


def fake_components(_: object) -> CrewAIComponents:
    return CrewAIComponents(
        Agent=FakeAgent,
        Crew=FakeCrew,
        Task=FakeTask,
        Process=FakeProcess,
        LLM=FakeLLM,
    )


def test_executor_builds_a_non_delegating_sequential_crew() -> None:
    executor = CrewAIExecutor(
        provider="openai",
        model="openai/gpt-5.6-terra",
        component_loader=fake_components,
    )

    result = executor.run(
        role="Analyst",
        goal="Analyze safely",
        backstory="A careful agent.",
        prompt="Return a result.",
        expected_output="Result JSON.",
        output_model=Result,
    )

    assert result == Result(value="crew result")
    assert FakeCrew.created is not None
    agent = FakeCrew.created["agents"][0]
    task = FakeCrew.created["tasks"][0]
    assert isinstance(agent, FakeAgent)
    assert agent.kwargs["allow_delegation"] is False
    assert agent.kwargs["memory"] is False
    assert agent.kwargs["llm"].kwargs == {"model": "gpt-5.6-terra"}
    assert isinstance(task, FakeTask)
    assert task.kwargs["output_pydantic"] is Result
    assert FakeCrew.created["process"] == "sequential"
    assert FakeCrew.created["memory"] is False


def test_executor_passes_temperature_for_models_that_support_it() -> None:
    executor = CrewAIExecutor(
        provider="openai",
        model="gpt-4o-mini",
        temperature=0.2,
        component_loader=fake_components,
    )

    executor.run(
        role="Analyst",
        goal="Analyze safely",
        backstory="A careful agent.",
        prompt="Return a result.",
        expected_output="Result JSON.",
        output_model=Result,
    )

    assert FakeCrew.created is not None
    agent = FakeCrew.created["agents"][0]
    assert isinstance(agent, FakeAgent)
    assert agent.kwargs["llm"].kwargs == {"model": "gpt-4o-mini", "temperature": 0.2}


def test_real_crewai_types_construct_without_a_model_call(monkeypatch) -> None:
    """Exercise the installed CrewAI API without a key or network request."""
    pytest.importorskip("crewai", reason="CrewAI is not installed in this environment")
    # CrewAI auto-loads a nearby `.env`; keep this construction-only test from
    # inheriting a developer's real credential.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    storage_root = Path.cwd() / ".reforge-crewai-test"
    components = _load_crewai(storage_root)
    # A local Ollama model gives CrewAI an LLM object without touching a real
    # provider; this test never calls the model.
    llm = components.LLM(model="ollama/llama3.2")
    agent = components.Agent(
        role="Verifier",
        goal="Check a contract",
        backstory="Test-only agent.",
        llm=llm,
        allow_delegation=False,
        verbose=False,
        memory=False,
    )
    task = components.Task(
        description="Return a contract-compliant result.",
        expected_output="One Result JSON object.",
        agent=agent,
        output_pydantic=Result,
    )
    crew = components.Crew(
        agents=[agent],
        tasks=[task],
        process=components.Process.sequential,
        verbose=False,
        memory=False,
        share_crew=False,
    )

    assert task.output_pydantic is Result
    assert crew.process == components.Process.sequential
    assert storage_root.is_dir()
