import json
from pathlib import Path

from reforge.backend.app.agents.transformation import TransformationAgent
from reforge.backend.app.core.guards import MutationGuards
from reforge.backend.app.core.state import PlanStep


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class FakeRepository:
    def __init__(self, _: Path) -> None:
        self.applied: list[str] = []

    def read_file(self, path: str) -> str:
        return "old_api()" if path == "legacy.py" else ""

    def apply_patch(self, unified_diff: str) -> None:
        self.applied.append(unified_diff)


class FakeGit:
    diff_value = ""

    def __init__(self, _: Path) -> None:
        self.commits: list[tuple[str, list[str] | None]] = []

    def diff(self) -> str:
        return self.diff_value

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        self.commits.append((message, paths))
        return "abc123"


def valid_patch(path: str = "legacy.py") -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-old_api()
+new_api()
"""


def payload(diff: str) -> str:
    return json.dumps({"summary": "Replace deprecated API", "patches": [{"unified_diff": diff}]})


def step() -> PlanStep:
    return PlanStep(
        id="S1",
        title="Replace deprecated API",
        description="Replace the old call.",
        files=["legacy.py"],
        depends_on=[],
        kind="api_replacement",
        validation_hint="Tests pass.",
    )


def test_transformation_applies_guarded_patch_and_commits() -> None:
    FakeGit.diff_value = valid_patch()
    repository = FakeRepository(Path("."))
    agent = TransformationAgent(
        FakeLLM([payload(valid_patch())]),
        repository_factory=lambda _: repository,
        git_factory=FakeGit,
    )

    result = agent.run(
        objective="Replace old API",
        step=step(),
        workspace=Path("reforge/backend/tests/fixtures/dependency_repo"),
        guards=MutationGuards(protected_test_files={"tests/test_legacy.py"}),
    )

    assert result.status == "applied"
    assert result.files_changed == ["legacy.py"]
    assert result.commit_sha == "abc123"
    assert repository.applied == [valid_patch()]


def test_transformation_reprompts_once_after_a_guard_rejection() -> None:
    rejected = valid_patch("tests/test_legacy.py")
    FakeGit.diff_value = valid_patch()
    repository = FakeRepository(Path("."))
    llm = FakeLLM([payload(rejected), payload(valid_patch())])
    agent = TransformationAgent(
        llm,
        repository_factory=lambda _: repository,
        git_factory=FakeGit,
    )

    result = agent.run(
        objective="Replace old API",
        step=step(),
        workspace=Path("reforge/backend/tests/fixtures/dependency_repo"),
        guards=MutationGuards(protected_test_files={"tests/test_legacy.py"}),
    )

    assert result.status == "applied"
    assert len(llm.prompts) == 2
    assert "protected baseline tests" in llm.prompts[1]
    assert repository.applied == [valid_patch()]
