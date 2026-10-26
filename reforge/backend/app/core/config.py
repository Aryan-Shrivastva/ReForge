"""Configuration loaded from environment variables.

The values here deliberately mirror Section 13 of the workflow specification.
Validation lives in ``Settings.__post_init__`` so that every construction path
(environment, API ``config_overrides``, tests) enforces the same bounds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


LLM_PROVIDERS = frozenset({"anthropic", "openai", "ollama", "gemini"})
AGENT_FRAMEWORKS = frozenset({"crewai", "adk", "hybrid"})


def _load_local_environment() -> None:
    """Load the ignored local `.env` file without overriding real environment values."""
    configured_path = os.getenv("REFORGE_ENV_FILE")
    dotenv_path = Path(configured_path) if configured_path else Path.cwd() / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=False)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None else int(value)


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
    # Which agent runtime executes the reasoning agents.  ``hybrid`` runs the
    # analysis/planning agents on Google ADK (with a per-run session whose state
    # carries run memory) and the patch-producing agents on CrewAI.
    agent_framework: str = "crewai"
    # Optional SQLAlchemy URL for a persistent ADK session store; in-memory when unset.
    adk_session_db_url: str | None = None
    max_upload_bytes: int = 200 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 1024 * 1024 * 1024
    max_archive_members: int = 50_000
    max_concurrent_runs: int = 2
    # Human-in-the-loop: pause every run for plan approval before changing code.
    require_plan_approval: bool = True
    # MCP: agents and the analysis step reach ReForge's tools through its MCP server.
    enable_mcp: bool = True
    # A2A: base URL of the ADK planner's A2A server; when set, planning runs over A2A.
    a2a_planner_url: str | None = None
    # Cost estimates: US dollars per one million tokens (0 = not configured / free tier).
    llm_input_price_per_mtok: float = 0.0
    llm_output_price_per_mtok: float = 0.0

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            expected = _FIELD_TYPES[field.name]
            if value is None and None in expected:
                continue
            # bool is a subclass of int; reject it where an integer is expected.
            if isinstance(value, bool) and bool not in expected:
                raise ValueError(f"{field.name} must be {_describe(expected)}")
            if not isinstance(value, tuple(item for item in expected if item is not None)):
                raise ValueError(f"{field.name} must be {_describe(expected)}")
        for name in (
            "max_repairs",
            "max_no_progress",
            "max_replans",
            "max_tokens",
            "max_wall_clock_s",
            "min_baseline_passing_tests",
            "sandbox_cpus",
            "sandbox_cmd_timeout_s",
            "max_upload_bytes",
            "max_archive_uncompressed_bytes",
            "max_archive_members",
            "max_concurrent_runs",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.llm_input_price_per_mtok < 0 or self.llm_output_price_per_mtok < 0:
            raise ValueError("LLM prices must not be negative")
        if self.a2a_planner_url is not None and not self.a2a_planner_url.startswith(("http://", "https://")):
            raise ValueError("A2A_PLANNER_URL must be an http(s) URL")
        if not 0 <= self.llm_temperature <= 2:
            raise ValueError("LLM_TEMPERATURE must be between 0 and 2")
        if self.llm_provider not in LLM_PROVIDERS:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(LLM_PROVIDERS)}")
        if self.agent_framework not in AGENT_FRAMEWORKS:
            raise ValueError(f"AGENT_FRAMEWORK must be one of {sorted(AGENT_FRAMEWORKS)}")

    @classmethod
    def from_env(cls) -> "Settings":
        """Construct settings without ever reading or logging provider secrets."""
        _load_local_environment()
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
            llm_provider=os.getenv("LLM_PROVIDER", "openai").lower(),
            llm_model=os.getenv("LLM_MODEL"),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
            allow_dependency_rollback_fallback=_env_bool("ALLOW_DEPENDENCY_ROLLBACK_FALLBACK", False),
            agent_framework=os.getenv("AGENT_FRAMEWORK", "crewai").lower(),
            adk_session_db_url=os.getenv("ADK_SESSION_DB_URL") or None,
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024),
            max_archive_uncompressed_bytes=_env_int("MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1024 * 1024 * 1024),
            max_archive_members=_env_int("MAX_ARCHIVE_MEMBERS", 50_000),
            max_concurrent_runs=_env_int("MAX_CONCURRENT_RUNS", 2),
            require_plan_approval=_env_bool("REQUIRE_PLAN_APPROVAL", True),
            enable_mcp=_env_bool("ENABLE_MCP", True),
            a2a_planner_url=(os.getenv("A2A_PLANNER_URL") or "").strip() or None,
            llm_input_price_per_mtok=float(os.getenv("LLM_INPUT_PRICE_PER_MTOK", "0") or 0),
            llm_output_price_per_mtok=float(os.getenv("LLM_OUTPUT_PRICE_PER_MTOK", "0") or 0),
        )

    def with_overrides(self, overrides: dict[str, Any]) -> "Settings":
        """Return validated settings with per-run overrides; unknown keys are rejected."""
        permitted = {field.name for field in fields(self)} - _NOT_OVERRIDABLE
        unknown = set(overrides) - permitted
        if unknown:
            raise ValueError(f"unsupported config overrides: {sorted(unknown)}")
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        # JSON has no float/int distinction for whole numbers; accept 1 for 1.0.
        if isinstance(overrides.get("llm_temperature"), int) and not isinstance(overrides["llm_temperature"], bool):
            overrides = {**overrides, "llm_temperature": float(overrides["llm_temperature"])}
        values.update(overrides)
        return Settings(**values)


# Infrastructure endpoints and prices are deployment settings: a per-run API
# override must never redirect the backend to another host or database.
_NOT_OVERRIDABLE = frozenset({"a2a_planner_url", "adk_session_db_url", "llm_input_price_per_mtok", "llm_output_price_per_mtok"})

_FIELD_TYPES: dict[str, tuple[Any, ...]] = {
    "max_repairs": (int,),
    "max_no_progress": (int,),
    "max_replans": (int,),
    "max_tokens": (int,),
    "max_wall_clock_s": (int,),
    "min_baseline_passing_tests": (int,),
    "sandbox_cpus": (int,),
    "sandbox_mem": (str,),
    "sandbox_cmd_timeout_s": (int,),
    "llm_provider": (str,),
    "llm_model": (str, None),
    "llm_temperature": (float, int),
    "allow_dependency_rollback_fallback": (bool,),
    "agent_framework": (str,),
    "adk_session_db_url": (str, None),
    "max_upload_bytes": (int,),
    "max_archive_uncompressed_bytes": (int,),
    "max_archive_members": (int,),
    "max_concurrent_runs": (int,),
    "require_plan_approval": (bool,),
    "enable_mcp": (bool,),
    "a2a_planner_url": (str, None),
    "llm_input_price_per_mtok": (float, int),
    "llm_output_price_per_mtok": (float, int),
}


def _describe(expected: tuple[Any, ...]) -> str:
    names = ["null" if item is None else item.__name__ for item in expected]
    return " or ".join(names)
