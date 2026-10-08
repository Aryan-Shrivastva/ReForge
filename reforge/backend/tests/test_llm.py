import pytest

from reforge.backend.app.core.config import Settings
from reforge.backend.app.llm import LLMConfigurationError, OllamaLLM, build_llm


def test_builds_selected_provider_without_reading_a_secret() -> None:
    client = build_llm(Settings(llm_provider="ollama", llm_model="qwen-test"))

    assert isinstance(client, OllamaLLM)
    assert client.model == "qwen-test"


def test_rejects_run_without_a_model() -> None:
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL"):
        build_llm(Settings(llm_provider="ollama", llm_model=None))
