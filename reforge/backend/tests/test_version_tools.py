from reforge.backend.app.tools.interfaces import ExecResult
from reforge.backend.app.tools.version_tools import check_resolution


class FakeSandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], str, int]] = []

    def run(self, cmd: list[str], phase: str, timeout: int) -> ExecResult:
        self.calls.append((cmd, phase, timeout))
        return self.result


def test_resolution_uses_network_enabled_install_phase() -> None:
    sandbox = FakeSandbox(ExecResult(0, "would install", "", 0.1, False))

    result = check_resolution(sandbox, ["Flask>=3.0"], timeout_s=42)

    assert result.ok is True
    assert sandbox.calls == [
        (["python", "-m", "pip", "install", "--dry-run", "Flask>=3.0"], "install", 42)
    ]
