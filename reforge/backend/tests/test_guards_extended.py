"""Guard coverage for Python and JavaScript/TypeScript test-weakening routes."""

from __future__ import annotations

import pytest

from reforge.backend.app.core.guards import GuardViolation, MutationGuards, changed_paths


def patch(path: str, removed: str, added: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{removed}\n+{added}\n"


def new_file(path: str, content: str) -> str:
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{content}\n"


@pytest.mark.parametrize(
    "added",
    [
        "@pytest.mark.skipif(True, reason='x')",
        "pytest.skip('flaky')",
        "@unittest.skip('later')",
        "self.skipTest('later')",
        "from unittest import skip",
    ],
)
def test_python_skip_variants_are_rejected(added: str) -> None:
    with pytest.raises(GuardViolation, match="skip"):
        MutationGuards().validate_transformation_patch(patch("legacy.py", "x = 1", added), ["legacy.py"])


@pytest.mark.parametrize(
    "added",
    [
        "it.skip('works', () => {})",
        "describe.only('suite', () => {})",
        "test.todo('later')",
        "xit('works', () => {})",
        "fdescribe('suite', () => {})",
        "this.skip();",
    ],
)
def test_javascript_skip_and_focus_markers_are_rejected(added: str) -> None:
    with pytest.raises(GuardViolation, match="marker"):
        MutationGuards().validate_repair_patch(patch("src/app.js", "const x = 1;", added))


def test_ordinary_calls_named_like_markers_are_allowed() -> None:
    guards = MutationGuards()

    guards.validate_transformation_patch(patch("model.py", "m.train(x)", "model.fit(x)"), ["model.py"])
    guards.validate_transformation_patch(patch("src/ml.js", "m.train(x);", "model.fit(x);"), ["src/ml.js"])


@pytest.mark.parametrize(
    "path",
    ["conftest.py", "src/conftest.py", "sitecustomize.py", "evil.pth", "jest.setup.js", "src/setupTests.ts",
     "tests/helpers.py", "src/__tests__/util.js", "src/app.test.js"],
)
def test_new_test_infrastructure_files_are_rejected(path: str) -> None:
    with pytest.raises(GuardViolation, match="test infrastructure"):
        MutationGuards().validate_transformation_patch(new_file(path, "x = 1"), ["src/app.py"])
    with pytest.raises(GuardViolation, match="test infrastructure"):
        MutationGuards().validate_repair_patch(new_file(path, "x = 1"))


def test_new_source_files_remain_allowed() -> None:
    assert MutationGuards().validate_transformation_patch(new_file("src/compat.js", "module.exports = {};"), ["src/app.js"]) == {
        "src/compat.js"
    }


@pytest.mark.parametrize(
    ("path", "added"),
    [
        ("package.json", '  "test": "jest --passWithNoTests",'),
        ("package.json", '  "test": "jest || true",'),
        ("jest.config.js", "  testPathIgnorePatterns: ['/legacy/'],"),
        ("vitest.config.ts", "    exclude: ['tests/slow/**'],"),
        ("pyproject.toml", 'addopts = "--deselect tests/test_a.py::test_x"'),
        ("pytest.ini", "testpaths = src"),
    ],
)
def test_test_selection_weakening_in_configuration_is_rejected(path: str, added: str) -> None:
    with pytest.raises(GuardViolation, match="test selection"):
        MutationGuards().validate_repair_patch(patch(path, "  \"name\": \"x\",", added))


def test_dependency_upgrades_in_package_json_are_allowed() -> None:
    guards = MutationGuards(prior_dependency_constraints={"express": "^4.17.1"})

    guards.validate_transformation_patch(
        patch("package.json", '    "express": "^4.17.1",', '    "express": "^5.1.0",'), ["package.json"]
    )


def test_dependency_rollback_requires_name_and_exact_constraint() -> None:
    guards = MutationGuards(prior_dependency_constraints={"express": "^4.17.1", "requests": "2.0"})

    with pytest.raises(GuardViolation, match="rollback"):
        guards.validate_repair_patch(patch("package.json", '"express": "^5.1.0",', '"express": "^4.17.1",'))
    # "2.0" inside "2.0.1", or on an unrelated dependency's line, is not a rollback.
    guards.validate_repair_patch(patch("requirements.txt", "requests==2.31", "requests==2.0.1"))
    guards.validate_repair_patch(patch("requirements.txt", "urllib3==1.0", "urllib3==2.0"))


def test_javascript_assertion_removal_in_test_files_only() -> None:
    guards = MutationGuards()

    with pytest.raises(GuardViolation, match="assertion"):
        guards._reject_test_weakening(patch("src/app.test.js", "expect(x).toBe(1);", "x;"))
    # Removing an assert() call from production code is a normal refactor.
    guards.validate_repair_patch(patch("src/server.js", "assert(config.port);", "validate(config);"))


def test_diff_paths_with_spaces_and_git_quoting_are_parsed() -> None:
    spaced = "--- a/src/my module.py\n+++ b/src/my module.py\n@@ -1 +1 @@\n-a\n+b\n"
    quoted = '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n-a\n+b\n'
    timestamped = "--- a/app.py\t2024-01-01 00:00:00\n+++ b/app.py\t2024-01-01 00:00:00\n@@ -1 +1 @@\n-a\n+b\n"

    assert changed_paths(spaced) == {"src/my module.py"}
    assert changed_paths(quoted) == {"café.py"}
    assert changed_paths(timestamped) == {"app.py"}


def test_backslash_paths_are_rejected() -> None:
    with pytest.raises(GuardViolation, match="unsafe"):
        changed_paths("--- a/..\\evil.py\n+++ b/..\\evil.py\n")


def test_moving_dependencies_between_manifests_at_unchanged_versions_is_not_a_rollback() -> None:
    """CharitEase run fd3abf18: consolidating two package.json files was blocked as a "rollback"."""
    guards = MutationGuards(prior_dependency_constraints={"ejs": "^3.1.10", "express": "^4.21.1", "nodemon": "^3.1.7"})
    consolidate = (
        "--- a/back-end/package.json\n+++ b/back-end/package.json\n@@ -2,5 +2,2 @@\n"
        '   "dependencies": {\n-    "ejs": "^3.1.10",\n-    "express": "^4.21.1"\n   }\n'
        "--- a/package.json\n+++ b/package.json\n@@ -10,4 +10,6 @@\n"
        '   "dependencies": {\n+    "ejs": "^3.1.10",\n+    "express": "^4.21.1",\n     "nodemon": "^3.1.7"\n   }\n'
    )
    move_to_dev = (
        "--- a/package.json\n+++ b/package.json\n@@ -12,3 +12,5 @@\n"
        '-    "nodemon": "^3.1.7"\n+  },\n+  "devDependencies": {\n+    "nodemon": "^3.1.7"\n'
    )

    guards.validate_transformation_patch(consolidate, ["package.json", "back-end/package.json"])
    guards.validate_transformation_patch(move_to_dev, ["package.json"])


def test_replacing_an_upgraded_version_with_the_original_is_still_a_rollback() -> None:
    guards = MutationGuards(prior_dependency_constraints={"express": "^4.21.1"})
    rollback = 'diff\n--- a/package.json\n+++ b/package.json\n@@ -3 +3 @@\n-    "express": "^5.1.0",\n+    "express": "^4.21.1",\n'

    with pytest.raises(GuardViolation, match="rollback"):
        guards.validate_repair_patch(rollback)
