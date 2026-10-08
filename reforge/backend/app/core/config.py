"""Configuration loaded from environment variables.

The values here deliberately mirror Section 13 of the workflow specification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_local_environment() -> None:
    """Load the ignored local `.env` file without overriding real environment values."""
    configured_path = os.getenv("REFORGE_ENV_FILE")
    dotenv_path = Path(configured_path) if configured_path else Path.cwd() / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)


def _env_int(name: str, default: int) -> int:
    """Read a positive integer environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1")
    return parsed


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    max_repairs: int = 5
    max_no_progress: int = 2
    max_replans: int = 1
    max_tokens: int = 500_000
    max_wall_clock_s: int = 1_800
    min_baseline_passing_tests: int = 5
    sandbox_cpus: int = 2
    sandbox_mem: str = "2g"
    sandbox_cmd_timeout_s: int = 300
    llm_provider: str = "openai"
    llm_model: str | None = None
    llm_temperature: float = 0.1
    allow_dependency_rollback_fallback: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct settings without ever reading or logging provider secrets."""
        _load_local_environment()
        temperature = float(os.getenv("LLM_TEMPERATURE", "0.1"))
        if not 0 <= temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        provider = os.getenv("LLM_PROVIDER", "openai").lower()
        if provider not in {"anthropic", "openai", "ollama"}:
            raise ValueError("LLM_PROVIDER must be anthropic, openai, or ollama")
        return cls(
            max_repairs=_env_int("MAX_REPAIRS", 5),
            max_no_progress=_env_int("MAX_NO_PROGRESS", 2),
            max_replans=_env_int("MAX_REPLANS", 1),
            max_tokens=_env_int("MAX_TOKENS", 500_000),
            max_wall_clock_s=_env_int("MAX_WALL_CLOCK_S", 1_800),
            min_baseline_passing_tests=_env_int("MIN_BASELINE_PASSING_TESTS", 5),
            sandbox_cpus=_env_int("SANDBOX_CPUS", 2),
            sandbox_mem=os.getenv("SANDBOX_MEM", "2g"),
            sandbox_cmd_timeout_s=_env_int("SANDBOX_CMD_TIMEOUT_S", 300),
            llm_provider=provider,
            llm_model=os.getenv("LLM_MODEL"),
            llm_temperature=temperature,
            allow_dependency_rollback_fallback=_env_bool(
                "ALLOW_DEPENDENCY_ROLLBACK_FALLBACK", False
            ),
        )
