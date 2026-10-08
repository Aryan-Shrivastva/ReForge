"""Shared structured-output validation and retry mechanics for LLM agents."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..llm import LLMClient

ModelT = TypeVar("ModelT", bound=BaseModel)


class AgentOutputValidationError(RuntimeError):
    """Raised after an agent exhausts its schema-validation retries."""


class StructuredAgent:
    """Require JSON output and validate it before it can reach the orchestrator."""

    def __init__(self, llm: LLMClient, *, max_retries: int = 2) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        self.llm = llm
        self.max_retries = max_retries

    def complete_model(
        self,
        prompt: str,
        model: type[ModelT],
        validator: Callable[[ModelT], None] | None = None,
    ) -> ModelT:
        """Retry malformed or semantically invalid output up to the specified bound."""
        retry_note = ""
        errors: list[str] = []
        for attempt in range(self.max_retries + 1):
            raw = self.llm.complete(f"{prompt}{retry_note}")
            try:
                decoded = json.loads(self._strip_fence(raw))
                value = model.model_validate(decoded)
                if validator is not None:
                    validator(value)
                return value
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                errors.append(str(exc))
                if attempt < self.max_retries:
                    retry_note = (
                        "\n\nYour previous response was invalid. Return only corrected JSON matching "
                        f"the requested schema. Validation error: {exc}"
                    )
        raise AgentOutputValidationError(
            f"{model.__name__} validation failed after {self.max_retries + 1} attempts: {errors[-1]}"
        )

    @staticmethod
    def _strip_fence(raw: str) -> str:
        value = raw.strip()
        if value.startswith("```") and value.endswith("```"):
            value = value.split("\n", 1)[1].rsplit("\n", 1)[0]
        return value
