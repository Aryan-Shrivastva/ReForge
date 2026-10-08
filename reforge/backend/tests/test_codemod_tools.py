from pathlib import Path

import pytest

from reforge.backend.app.tools.codemod_tools import Codemod, CodemodTools
from reforge.backend.app.tools.interfaces import ExecResult


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str, int]] = []

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        self.calls.append((cmd, phase, timeout))
        return ExecResult(0, "", "", 0.1, False)


def test_codemod_tools_only_runs_allowlisted_command_on_declared_files() -> None:
    sandbox = FakeSandbox()
    tools = CodemodTools(sandbox, Path("reforge/backend/tests/fixtures/dependency_repo"))

    tools.run(Codemod.PYUPGRADE, ["setup.py"])

    assert sandbox.calls == [(["pyupgrade", "--py311-plus", "setup.py"], "run", 300)]


def test_codemod_tools_rejects_workspace_escape() -> None:
    tools = CodemodTools(FakeSandbox(), Path("reforge/backend/tests/fixtures/dependency_repo"))

    with pytest.raises(ValueError):
        tools.run(Codemod.RUFF, ["../../outside.py"])
