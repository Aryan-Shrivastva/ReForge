from pathlib import Path

from reforge.backend.app.tools.deps_tools import DependencyParser


def test_parses_supported_dependency_files() -> None:
    root = Path("reforge/backend/tests/fixtures/dependency_repo")

    dependencies = DependencyParser(root).parse()
    found = {(item.name, item.constraint) for item in dependencies}

    assert ("flask", "==1.1.4") in found
    assert ("pydantic", ">=1.10") in found
    assert ("click", ">=8") in found
    assert ("uvicorn", "==0.30.0") in found
    assert ("unversioned", None) in found
