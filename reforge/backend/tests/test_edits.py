"""Search/replace edits: applied exactly once, validated by the guards, robust to model mistakes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reforge.backend.app.agents.transformation import TransformationAgent
from reforge.backend.app.core.guards import GuardViolation, MutationGuards
from reforge.backend.app.core.state import FileEdit, PlanStep, RepairProposal
from reforge.backend.app.tools.edits import EditError, plan_edits, write_edits
from reforge.backend.app.tools.repo_tools import RepositoryTools

PACKAGE = """{
  "name": "charitease",
  "engines": {
    "node": ">=18"
  },
  "dependencies": {
    "ejs": "^3.1.10",
    "express": "^4.21.1",
    "nodemon": "^3.1.7"
  }
}
"""


def workspace(tmp_path: Path) -> RepositoryTools:
    (tmp_path / "package.json").write_text(PACKAGE, encoding="utf-8", newline="\n")
    return RepositoryTools(tmp_path)


def test_edit_applies_and_produces_a_guardable_diff(tmp_path: Path) -> None:
    repository = workspace(tmp_path)
    edit = FileEdit(
        path="package.json",
        search='    "express": "^4.21.1",\n    "nodemon": "^3.1.7"\n  }\n',
        replace='    "express": "^4.21.1"\n  },\n  "devDependencies": {\n    "nodemon": "^3.1.7"\n  }\n',
    )

    plan = plan_edits(repository, [edit])
    MutationGuards(prior_dependency_constraints={"nodemon": "^3.1.7"}).validate_transformation_patch(plan.diff, ["package.json"])
    write_edits(repository, plan)

    data = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
    assert data["devDependencies"] == {"nodemon": "^3.1.7"}
    assert "nodemon" not in data["dependencies"]
    assert plan.diff.startswith("--- a/package.json\n+++ b/package.json\n")


def test_edit_search_must_match_exactly_once(tmp_path: Path) -> None:
    repository = workspace(tmp_path)

    with pytest.raises(EditError, match="not found.*closest line"):
        plan_edits(repository, [FileEdit(path="package.json", search='"nodemon": "^3.1.8"', replace="x")])
    with pytest.raises(EditError, match="matches 3 places"):
        plan_edits(repository, [FileEdit(path="package.json", search='": "^', replace="x")])


def test_edits_handle_new_files_crlf_and_sequential_changes(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_bytes(b"const a = 1;\r\nconst b = 2;\r\n")
    repository = RepositoryTools(tmp_path)

    plan = plan_edits(repository, [
        FileEdit(path="app.js", search="const a = 1;\nconst b = 2;", replace="const a = 1;\nconst b = 3;"),
        FileEdit(path="app.js", search="const b = 3;", replace="const b = 4;"),
        FileEdit(path="lib/new.js", search="", replace="module.exports = {};\n"),
    ])
    write_edits(repository, plan)

    assert (tmp_path / "app.js").read_bytes() == b"const a = 1;\r\nconst b = 4;\r\n"
    assert (tmp_path / "lib" / "new.js").read_text(encoding="utf-8") == "module.exports = {};\n"
    assert plan.created == {"lib/new.js"} and "--- /dev/null" in plan.diff


def test_edits_cannot_escape_the_workspace_or_weaken_tests(tmp_path: Path) -> None:
    repository = workspace(tmp_path)

    with pytest.raises(ValueError):
        plan_edits(repository, [FileEdit(path="../outside.js", search="", replace="x")])
    plan = plan_edits(repository, [FileEdit(path="tests/conftest.py", search="", replace="import pytest\n")])
    with pytest.raises(GuardViolation, match="test infrastructure"):
        MutationGuards().validate_transformation_patch(plan.diff, ["package.json"])


def test_repair_proposal_requires_some_change() -> None:
    with pytest.raises(ValueError, match="at least one edit or patch"):
        RepairProposal(diagnosis="x", root_cause_category="logic")


class FixedExecutor:
    framework = "crewai"

    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal

    def run(self, *, output_model, **_: object):
        return output_model.model_validate(self.proposal)


class RecordingGit:
    def __init__(self, _: Path) -> None:
        self.committed: list[str] = []

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        self.committed = list(paths or [])
        return "c" * 40

    def diff(self, revision: str | None = None) -> str:
        return ""


def test_transformation_agent_applies_edits_end_to_end(tmp_path: Path) -> None:
    workspace(tmp_path)
    git = RecordingGit(tmp_path)
    agent = TransformationAgent(
        executor=FixedExecutor({
            "summary": "Move nodemon to devDependencies",
            "edits": [{
                "path": "package.json",
                "search": '    "express": "^4.21.1",\n    "nodemon": "^3.1.7"\n  }\n',
                "replace": '    "express": "^4.21.1"\n  },\n  "devDependencies": {\n    "nodemon": "^3.1.7"\n  }\n',
            }],
        }),  # type: ignore[arg-type]
        git_factory=lambda _: git,  # type: ignore[arg-type,return-value]
    )
    step = PlanStep(id="S2", title="Move nodemon", description="d", files=["package.json"], kind="dependency", validation_hint="v")

    result = agent.run(objective="o", step=step, workspace=tmp_path, guards=MutationGuards(), codemods=None)

    assert result.status == "applied", result.error
    assert git.committed == ["package.json"]
    assert '"devDependencies"' in (tmp_path / "package.json").read_text(encoding="utf-8")
