from reforge.backend.app.core.state import ProjectState


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

