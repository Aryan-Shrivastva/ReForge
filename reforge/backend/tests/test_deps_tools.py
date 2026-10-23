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


def test_parses_gradle_dependencies_plugins_and_aliases(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "build.gradle.kts").write_text(
        'plugins { id("com.google.gms.google-services") version "4.4.2" apply false }\n',
        encoding="utf-8",
    )
    (tmp_path / "app" / "build.gradle.kts").write_text(
        '''
        dependencies {
            implementation("com.google.code.gson:gson:2.10.1")
            implementation(platform("com.google.firebase:firebase-bom:33.13.0"))
            implementation(libs.appcompat)
            val lottieVersion = "3.4.0"
            implementation("com.airbnb.android:lottie:$lottieVersion")
        }
        ''',
        encoding="utf-8",
    )

    found = {(item.name, item.constraint, item.source) for item in DependencyParser(tmp_path).parse()}

    assert ("com.google.code.gson:gson", "2.10.1", "app/build.gradle.kts") in found
    assert ("com.google.firebase:firebase-bom", "33.13.0", "app/build.gradle.kts") in found
    assert ("com.airbnb.android:lottie", "3.4.0", "app/build.gradle.kts") in found
    assert ("libs.appcompat", None, "app/build.gradle.kts") in found
    assert ("plugin:com.google.gms.google-services", "4.4.2", "build.gradle.kts") in found


def test_parses_npm_dependency_ranges(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react":"^18.3.0"},"devDependencies":{"vite":"^5.0.0"}}',
        encoding="utf-8",
    )

    found = {(item.name, item.constraint, item.source) for item in DependencyParser(tmp_path).parse()}

    assert ("react", "^18.3.0", "package.json (dependencies)") in found
    assert ("vite", "^5.0.0", "package.json (devDependencies)") in found
