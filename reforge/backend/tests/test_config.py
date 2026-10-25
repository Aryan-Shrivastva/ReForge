import pytest

from reforge.backend.app.core.config import Settings


def test_defaults_match_workflow_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFORGE_ENV_FILE", ".missing-test.env")
    for name in ("MAX_REPAIRS", "LLM_PROVIDER", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.max_repairs == 5
    assert settings.max_no_progress == 2
    assert settings.max_replans == 1
    assert settings.max_tokens == 500_000
    assert settings.llm_provider == "openai"


def test_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFORGE_ENV_FILE", ".missing-test.env")
    monkeypatch.setenv("LLM_PROVIDER", "unknown")

    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        Settings.from_env()


def test_overrides_are_type_checked_and_bounded() -> None:
    settings = Settings()

    assert settings.with_overrides({"max_repairs": 2, "llm_temperature": 1}).llm_temperature == 1.0
    for overrides in ({"max_repairs": "2"}, {"max_repairs": True}, {"max_tokens": 0}, {"agent_framework": "x"}):
        with pytest.raises(ValueError):
            settings.with_overrides(overrides)
    with pytest.raises(ValueError, match="unsupported"):
        settings.with_overrides({"docker_socket": "/var/run/docker.sock"})


def test_agent_framework_and_gemini_provider_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFORGE_ENV_FILE", ".missing-test.env")
    monkeypatch.setenv("AGENT_FRAMEWORK", "Hybrid")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("ADK_SESSION_DB_URL", "sqlite:///runs/adk.db")

    settings = Settings.from_env()

    assert settings.agent_framework == "hybrid"
    assert settings.llm_provider == "gemini"
    assert settings.adk_session_db_url == "sqlite:///runs/adk.db"
