"""Provider-neutral LLM clients selected through ReForge configuration."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .core.config import Settings


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str:
        """Return one model completion. Implementations must not expose secrets."""


class LLMConfigurationError(RuntimeError):
    """Raised when a selected provider has not been configured safely."""


class HttpLLMClient:
    """Minimal HTTP client shared by hosted and local provider adapters."""

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        try:
            with urlopen(request, timeout=120) as response:  # noqa: S310 - provider URL is configured by user.
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("LLM provider returned a non-object JSON response")
        return payload


class OpenAICompatibleLLM(HttpLLMClient):
    def __init__(self, model: str, *, base_url: str | None = None, api_key: str | None = None, temperature: float = 0.1) -> None:
        self.model = model
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.temperature = temperature

    def complete(self, prompt: str) -> str:
        if not self.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is required for the OpenAI-compatible provider")
        payload = self._post_json(
            f"{self.base_url}/chat/completions",
            {"Authorization": f"Bearer {self.api_key}"},
            {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": self.temperature},
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI-compatible response did not contain choices[0].message.content") from exc
        if not isinstance(content, str):
            raise RuntimeError("OpenAI-compatible response content was not text")
        return content


class AnthropicLLM(HttpLLMClient):
    def __init__(self, model: str, *, api_key: str | None = None, temperature: float = 0.1) -> None:
        self.model = model
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.temperature = temperature

    def complete(self, prompt: str) -> str:
        if not self.api_key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is required for the Anthropic provider")
        payload = self._post_json(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": 8_000, "temperature": self.temperature, "messages": [{"role": "user", "content": prompt}]},
        )
        content = payload.get("content")
        if not isinstance(content, list):
            raise RuntimeError("Anthropic response did not contain content blocks")
        text = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
        if not text:
            raise RuntimeError("Anthropic response did not contain text")
        return text


class OllamaLLM(HttpLLMClient):
    def __init__(self, model: str, *, base_url: str | None = None, temperature: float = 0.1) -> None:
        self.model = model
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.temperature = temperature

    def complete(self, prompt: str) -> str:
        payload = self._post_json(
            f"{self.base_url}/api/generate",
            {"Accept": "application/json"},
            {"model": self.model, "prompt": prompt, "stream": False, "options": {"temperature": self.temperature}},
        )
        response = payload.get("response")
        if not isinstance(response, str):
            raise RuntimeError("Ollama response did not contain text")
        return response


def build_llm(settings: Settings) -> LLMClient:
    """Create the selected provider client; credentials stay in environment variables."""
    if not settings.llm_model:
        raise LLMConfigurationError("LLM_MODEL must be configured before starting a run")
    if settings.llm_provider == "openai":
        return OpenAICompatibleLLM(settings.llm_model, temperature=settings.llm_temperature)
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings.llm_model, temperature=settings.llm_temperature)
    if settings.llm_provider == "ollama":
        return OllamaLLM(settings.llm_model, temperature=settings.llm_temperature)
    raise LLMConfigurationError(f"Unsupported LLM provider: {settings.llm_provider}")
