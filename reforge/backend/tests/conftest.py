import os

# Keep the suite independent of a developer's local .env: importing the app
# calls Settings.from_env(), whose load_dotenv() would otherwise copy provider
# settings (OPENAI_BASE_URL, keys, model) into os.environ for every test.
os.environ["REFORGE_ENV_FILE"] = os.path.join(os.path.dirname(__file__), ".no-local-env")
for _name in (
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OLLAMA_BASE_URL", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "LLM_PROVIDER", "LLM_MODEL", "AGENT_FRAMEWORK", "ADK_SESSION_DB_URL",
):
    os.environ.pop(_name, None)

from reforge.backend.app.core.state import ProjectState  # noqa: E402


def make_state(**overrides: object) -> ProjectState:
    values: dict[str, object] = {
        "run_id": "run-001",
        "repo_source": "https://example.invalid/legacy.git",
        "objective": "Upgrade a dependency",
        "workspace_path": "/workspace/run-001",
        "work_branch": "reforge/run-001",
        "base_commit": "a" * 40,
    }
    values.update(overrides)
    return ProjectState(**values)

