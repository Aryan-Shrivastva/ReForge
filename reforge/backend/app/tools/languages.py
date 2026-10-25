"""Language and test-file classification shared by analysis, guards, and the workflow.

ReForge supports Python and JavaScript/TypeScript repositories as first-class
targets (with a narrow Java/Kotlin Android rule set).  Classification is purely
path-based so it can run on untrusted repositories without executing them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal

Language = Literal["python", "javascript", "typescript", "java", "kotlin"]
JsTestFramework = Literal["jest", "vitest", "mocha"]

SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".reforge-artifacts",
        ".gradle",
        "build",
        "dist",
        "coverage",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
    }
)

PYTHON_EXTENSIONS = (".py",)
JAVASCRIPT_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs")
TYPESCRIPT_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
JS_LIKE_EXTENSIONS = JAVASCRIPT_EXTENSIONS + TYPESCRIPT_EXTENSIONS

_JS_TEST_FILE = re.compile(r"\.(?:test|spec)\.[cm]?[jt]sx?$")
_TEST_DIRECTORIES = frozenset({"tests", "test", "__tests__", "spec"})
_PYTHON_TEST_INFRASTRUCTURE = frozenset({"conftest.py", "sitecustomize.py", "usercustomize.py"})
_JS_SETUP_FILE = re.compile(r"^(?:jest|vitest|mocha|karma)\.setup(?:\.[\w-]+)*\.[cm]?[jt]sx?$|^setupTests\.[cm]?[jt]sx?$")
_JS_TEST_CONFIG = re.compile(r"^(?:jest|vitest|vite|karma)\.config\.[cm]?[jt]s$|^\.mocharc(?:\.[\w]+)?$")
PYTHON_TEST_CONFIG_NAMES = frozenset({"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"})


def iter_source_files(workspace: Path, extensions: tuple[str, ...]) -> list[Path]:
    """Return matching files in sorted order, pruning skipped directories while walking.

    ``os.walk`` with pruning never descends into ``node_modules`` or virtual
    environments (which can be huge and contain POSIX symlinks that are unsafe
    to follow from a Windows host), and it does not follow symlinks.
    """
    root = workspace.resolve()
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if name not in SKIPPED_DIRECTORIES)
        for filename in filenames:
            if filename.lower().endswith(extensions):
                path = Path(directory) / filename
                if not path.is_symlink():
                    found.append(path)
    return sorted(found)


def is_skipped(relative: str) -> bool:
    """Whether a workspace-relative path lies in a generated, vendored, or tool directory."""
    return any(part in SKIPPED_DIRECTORIES for part in PurePosixPath(relative).parts[:-1])


def language_for(path: str) -> Language | None:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        return "python"
    if suffix in JAVASCRIPT_EXTENSIONS:
        return "javascript"
    if suffix in TYPESCRIPT_EXTENSIONS:
        return "typescript"
    if suffix == ".java":
        return "java"
    if suffix in {".kt", ".kts"}:
        return "kotlin"
    return None


def is_python_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_js_test_file(path: str) -> bool:
    pure = PurePosixPath(path)
    if not pure.name.endswith(JS_LIKE_EXTENSIONS):
        return False
    return bool(_JS_TEST_FILE.search(pure.name)) or "__tests__" in pure.parts[:-1]


def is_test_file(path: str) -> bool:
    """Whether a path is an executable test file in any supported language."""
    pure = PurePosixPath(path)
    if is_python_test_file(path) or is_js_test_file(path):
        return True
    if pure.suffix in {".java", ".kt"}:
        joined = f"/{path}"
        return "/src/test/" in joined or "/src/androidTest/" in joined
    return False


def is_test_infrastructure(path: str) -> bool:
    """Files that can change test collection or outcomes without being a test themselves.

    A mutation that adds or edits one of these could make failing tests pass
    (for example a new ``conftest.py`` that monkeypatches the code under test,
    a ``.pth``/``sitecustomize.py`` that runs on interpreter start, or a Jest
    setup file).  Transformations and repairs may never touch them.
    """
    pure = PurePosixPath(path)
    name = pure.name
    if name in _PYTHON_TEST_INFRASTRUCTURE or name.endswith(".pth"):
        return True
    if _JS_SETUP_FILE.match(name):
        return True
    if is_test_file(path):
        return True
    # Anything new under a test directory (helpers, fixtures, snapshots).
    return any(part in _TEST_DIRECTORIES for part in pure.parts[:-1])


def is_test_config(path: str) -> bool:
    """Configuration files that legitimately change but may not weaken test selection."""
    name = PurePosixPath(path).name
    return name in PYTHON_TEST_CONFIG_NAMES or name == "package.json" or bool(_JS_TEST_CONFIG.match(name))


def detect_languages(files: list[str]) -> list[Language]:
    """Return the languages present in a repository inventory, most common first."""
    counts: dict[Language, int] = {}
    for path in files:
        if is_skipped(path):
            continue
        language = language_for(path)
        if language is not None:
            counts[language] = counts.get(language, 0) + 1
    return sorted(counts, key=lambda language: (-counts[language], language))


def read_package_json(workspace: Path) -> dict[str, object]:
    """Read ``package.json`` as data; malformed or absent manifests are empty."""
    try:
        value = json.loads((workspace / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def package_scripts(package: dict[str, object]) -> dict[str, str]:
    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    return {str(name): value for name, value in scripts.items() if isinstance(value, str)}


def package_dependency_names(package: dict[str, object]) -> set[str]:
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = package.get(section, {})
        if isinstance(values, dict):
            names.update(str(name) for name in values)
    return names


def detect_js_test_framework(workspace: Path) -> JsTestFramework | None:
    """Detect a JSON-reporting test framework from the manifest and test script."""
    package = read_package_json(workspace)
    if not package:
        return None
    test_script = package_scripts(package).get("test", "")
    dependencies = package_dependency_names(package)
    for framework in ("vitest", "jest", "mocha"):
        if re.search(rf"\b{framework}\b", test_script):
            return framework  # type: ignore[return-value]
    for framework in ("vitest", "jest", "mocha"):
        if framework in dependencies:
            return framework  # type: ignore[return-value]
    if "jest" in package:
        return "jest"
    return None


def detect_js_package_manager(workspace: Path) -> Literal["npm", "pnpm", "yarn"]:
    if (workspace / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (workspace / "yarn.lock").is_file():
        return "yarn"
    return "npm"
