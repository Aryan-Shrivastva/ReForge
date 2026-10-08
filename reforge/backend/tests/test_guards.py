import pytest

from reforge.backend.app.core.guards import GuardViolation, MutationGuards, changed_paths


def patch(path: str, removed: str, added: str) -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-{removed}
+{added}
"""


def test_guard_accepts_declared_source_change() -> None:
    guards = MutationGuards(protected_test_files={"tests/test_legacy.py"})
    diff = patch("legacy.py", "old_api()", "new_api()")

    assert guards.validate_transformation_patch(diff, ["legacy.py"]) == {"legacy.py"}
    assert changed_paths(diff) == {"legacy.py"}


def test_guard_blocks_protected_tests_and_weakening() -> None:
    guards = MutationGuards(protected_test_files={"tests/test_legacy.py"})

    with pytest.raises(GuardViolation, match="protected"):
        guards.validate_transformation_patch(
            patch("tests/test_legacy.py", "assert value", "assert value == 1"),
            ["tests/test_legacy.py"],
        )

    with pytest.raises(GuardViolation, match="skip"):
        guards.validate_transformation_patch(
            patch("legacy.py", "value = 1", "pytest.mark.skip(reason='avoid failure')"),
            ["legacy.py"],
        )

    with pytest.raises(GuardViolation, match="removes a test assertion"):
        guards.validate_transformation_patch(
            patch("legacy.py", "assert value == 1", "value = 1"),
            ["legacy.py"],
        )


def test_guard_enforces_scope_and_new_file_exception() -> None:
    guards = MutationGuards()

    with pytest.raises(GuardViolation, match="outside"):
        guards.validate_transformation_patch(patch("other.py", "old", "new"), ["legacy.py"])

    new_file = """diff --git a/new_module.py b/new_module.py
new file mode 100644
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1 @@
+VALUE = 1
"""
    assert guards.validate_transformation_patch(new_file, ["legacy.py"]) == {"new_module.py"}


def test_guard_blocks_dependency_rollback_and_duplicate_repairs() -> None:
    guards = MutationGuards(prior_dependency_constraints={"flask": "Flask==1.1.4"})
    rollback = patch("requirements.txt", "Flask>=3", "Flask==1.1.4")

    with pytest.raises(GuardViolation, match="rollback"):
        guards.validate_transformation_patch(rollback, ["requirements.txt"])

    guards.register_repair_patch(patch("legacy.py", "old", "new"))
    with pytest.raises(GuardViolation, match="duplicate"):
        guards.register_repair_patch(patch("legacy.py", "old", "new"))


def test_repair_guard_blocks_broad_exception_handlers() -> None:
    guards = MutationGuards()

    with pytest.raises(GuardViolation, match="broad"):
        guards.validate_repair_patch(patch("legacy.py", "old", "except Exception:"))
