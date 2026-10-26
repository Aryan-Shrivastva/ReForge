import json
from pathlib import Path

import pytest

from reforge.backend.app.agents.repair import RepairAgent
from reforge.backend.app.agents.test_generation import (
    CharacterizationTestGenerator,
    CharacterizationPathError,
)
from reforge.backend.app.agents.base import AgentOutputValidationError
from reforge.backend.app.core.guards import GuardViolation, MutationGuards
from reforge.backend.app.core.state import Plan, PlanStep, TestReport as StateTestReport


class FakeLLM:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = json.dumps(response)

    def complete(self, _: str) -> str:
        return self.response


class SequencedLLM:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = [json.dumps(response) for response in responses]
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class FakeRepository:
    def __init__(self, _: Path) -> None:
        self.applied: list[str] = []

    def list_tree(self) -> list[str]:
        return ["legacy.py"]

    def read_file(self, _: str) -> str:
        return "def legacy():\n    return 1\n"

    def apply_patch(self, patch: str) -> None:
        self.applied.append(patch)

    def write_new_file(self, path: str, content: str) -> None:
        self.path = path
        self.content = content


class FakeGit:
    def __init__(self, _: Path) -> None:
        self.message = ""

    def diff(self, _: str | None = None) -> str:
        return ""

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        self.message = message
        self.paths = paths
        return "c" * 40


def repair_plan() -> Plan:
    return Plan(
        version=1,
        rationale="repair fixture",
        steps=[
            PlanStep(
                id="S1", title="Upgrade", description="Upgrade", files=["legacy.py"], depends_on=[],
                kind="api_replacement", validation_hint="tests pass"
            )
        ],
    )


def test_generator_only_writes_new_tests_in_dedicated_directory() -> None:
    repository = FakeRepository(Path("."))
    llm = SequencedLLM([{
        "summary": "Capture behavior",
        "files": [{"path": "tests/reforge_generated/test_legacy.py", "content": "def test_x():\n    assert 1\n"}],
    }])
    generator = CharacterizationTestGenerator(
        llm,
        repository_factory=lambda _: repository,  # type: ignore[arg-type]
    )

    proposal = generator.propose("Preserve behavior", Path("."))
    assert generator.write(Path("."), proposal) == ["tests/reforge_generated/test_legacy.py"]
    assert repository.path == "tests/reforge_generated/test_legacy.py"
    assert "public functions/classes" in llm.prompts[0]
    assert "object identity" in llm.prompts[0]


def test_generator_rejects_paths_outside_generated_test_directory() -> None:
    generator = CharacterizationTestGenerator(
        FakeLLM({"summary": "unsafe", "files": [{"path": "tests/test_old.py", "content": "pass\n"}]}),
        repository_factory=FakeRepository,  # type: ignore[arg-type]
    )

    with pytest.raises((AgentOutputValidationError, CharacterizationPathError, ValueError)):
        generator.propose("Preserve behavior", Path("."))


def test_generator_does_not_support_node_projects(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name": "web"}', encoding="utf-8")
    generator = CharacterizationTestGenerator(
        FakeLLM({"summary": "unused", "files": [{"path": "tests/reforge_generated/test_x.py", "content": "pass\n"}]}),
        repository_factory=FakeRepository,  # type: ignore[arg-type]
    )

    assert generator.supports(tmp_path) is False


def test_repair_applies_guarded_patch_and_commits() -> None:
    repository = FakeRepository(Path("."))
    git = FakeGit(Path("."))
    diff = "--- a/legacy.py\n+++ b/legacy.py\n@@ -1 +1 @@\n-old\n+new\n"
    agent = RepairAgent(
        FakeLLM({
            "diagnosis": "Adapt the renamed API",
            "root_cause_category": "api_change",
            "patches": [{"unified_diff": diff}],
        }),
        repository_factory=lambda _: repository,  # type: ignore[arg-type]
        git_factory=lambda _: git,  # type: ignore[arg-type]
    )
    report = StateTestReport(phase="post_transform", install_ok=True, duration_s=0)

    result = agent.run(
        objective="Upgrade", plan=repair_plan(), report=report, repairs=[], workspace=Path("."),
        guards=MutationGuards(protected_test_files={"tests/test_legacy.py"}), baseline_commit="a" * 40,
        failing_before=1,
    )

    assert result.commit_sha == "c" * 40
    assert result.failing_before == 1
    assert repository.applied == [diff]
    assert git.message.startswith("R1:")


def test_repair_guard_blocks_broad_except_before_application() -> None:
    repository = FakeRepository(Path("."))
    diff = "--- a/legacy.py\n+++ b/legacy.py\n@@ -1 +1 @@\n-old\n+except Exception:\n"
    agent = RepairAgent(
        FakeLLM({"diagnosis": "hide error", "root_cause_category": "logic", "patches": [{"unified_diff": diff}]}),
        repository_factory=lambda _: repository,  # type: ignore[arg-type]
        git_factory=FakeGit,  # type: ignore[arg-type]
    )
    with pytest.raises(GuardViolation, match="broad"):
        agent.run(
            objective="Upgrade", plan=repair_plan(), report=StateTestReport(phase="post_transform", install_ok=True, duration_s=0),
            repairs=[], workspace=Path("."), guards=MutationGuards(), baseline_commit="a" * 40,
            failing_before=1,
        )
    assert repository.applied == []


def test_repair_retries_an_unapplicable_patch_with_current_source_context() -> None:
    class RejectThenApplyRepository(FakeRepository):
        def apply_patch(self, patch: str) -> None:
            self.applied.append(patch)
            if len(self.applied) == 1:
                raise ValueError("patch rejected: error: patch does not apply")

    repository = RejectThenApplyRepository(Path("."))
    first = "--- a/legacy.py\n+++ b/legacy.py\n@@ -1 +1 @@\n-old\n+wrong\n"
    second = "--- a/legacy.py\n+++ b/legacy.py\n@@ -1 +1 @@\n-old\n+new\n"
    llm = SequencedLLM([
        {"diagnosis": "First attempt", "root_cause_category": "logic", "patches": [{"unified_diff": first}]},
        {"diagnosis": "Corrected attempt", "root_cause_category": "logic", "patches": [{"unified_diff": second}]},
    ])
    agent = RepairAgent(
        llm,
        repository_factory=lambda _: repository,  # type: ignore[arg-type]
        git_factory=FakeGit,  # type: ignore[arg-type]
    )

    result = agent.run(
        objective="Repair legacy API", plan=repair_plan(),
        report=StateTestReport(phase="post_transform", install_ok=True, duration_s=0),
        repairs=[], workspace=Path("."), guards=MutationGuards(), baseline_commit="a" * 40,
        failing_before=1,
    )

    assert result.diagnosis == "Corrected attempt"
    assert repository.applied == [first, second]
    assert '"legacy.py": "def legacy()' in llm.prompts[0]
    assert "previous repair was rejected" in llm.prompts[1]
    assert "patch does not apply" in llm.prompts[1]
