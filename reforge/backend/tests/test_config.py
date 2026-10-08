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
