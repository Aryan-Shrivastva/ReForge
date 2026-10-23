"""Dependency discovery from common project metadata without execution."""

from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement


@dataclass(frozen=True, slots=True)
class DeclaredDependency:
    name: str
    constraint: str | None
    source: str


class DependencyParser:
    """Parse declarations only; dependency resolution happens in the sandbox.

    The parser deliberately reports *declared* versions rather than pretending
    to know a safe upgrade target.  That keeps Android/Gradle evidence just as
    auditable as the existing Python metadata evidence.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"workspace must be an existing directory: {workspace}")

    def parse(self) -> list[DeclaredDependency]:
        found: list[DeclaredDependency] = []
        for path in sorted(self.workspace.glob("requirements*.txt")):
            found.extend(self._parse_requirements(path))
        requirements_dir = self.workspace / "requirements"
        if requirements_dir.is_dir():
            for path in sorted(requirements_dir.glob("*.txt")):
                found.extend(self._parse_requirements(path))
        for name, parser in (
            ("pyproject.toml", self._parse_pyproject),
            ("setup.cfg", self._parse_setup_cfg),
            ("setup.py", self._parse_setup_py),
            ("Pipfile", self._parse_pipfile),
        ):
            path = self.workspace / name
            if path.is_file():
                found.extend(parser(path))
        package_json = self.workspace / "package.json"
        if package_json.is_file():
            found.extend(self._parse_package_json(package_json))
        found.extend(self._parse_gradle_files())
        version_catalog = self.workspace / "gradle" / "libs.versions.toml"
        if version_catalog.is_file():
            found.extend(self._parse_gradle_version_catalog(version_catalog))
        return self._deduplicate(found)

    def _parse_gradle_files(self) -> list[DeclaredDependency]:
        """Read common Gradle dependency notations without running Gradle."""
        dependencies: list[DeclaredDependency] = []
        ignored = {".git", ".venv", "build", ".gradle"}
        gradle_files = sorted(
            path
            for pattern in ("build.gradle", "build.gradle.kts")
            for path in self.workspace.rglob(pattern)
            if not any(part in ignored for part in path.relative_to(self.workspace).parts)
        )
        for path in gradle_files:
            source = path.relative_to(self.workspace).as_posix()
            text = path.read_text(encoding="utf-8")
            variables = {
                match.group("name"): match.group("value")
                for match in re.finditer(
                    r"(?:val|var)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*[\"'](?P<value>[^\"']+)[\"']",
                    text,
                )
            }
            # `implementation("group:artifact:version")` and the platform
            # variant used by Firebase BOMs.
            for match in re.finditer(
                r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation|"
                r"annotationProcessor|kapt)\s*\(\s*(?:platform\s*\(\s*)?[\"'](?P<coordinate>[^\"']+)[\"']",
                text,
            ):
                dependency = self._from_gradle_coordinate(
                    match.group("coordinate"), source, variables
                )
                if dependency is not None:
                    dependencies.append(dependency)
            # Version-catalog aliases are useful evidence even when a project
            # accidentally omitted its catalog file; they should not vanish.
            for match in re.finditer(
                r"(?:implementation|api|compileOnly|runtimeOnly|testImplementation|androidTestImplementation)"
                r"\s*\(\s*(libs\.[A-Za-z0-9_.]+)\s*\)",
                text,
            ):
                dependencies.append(DeclaredDependency(match.group(1), None, source))
            for match in re.finditer(
                r"id\(\s*[\"'](?P<name>[^\"']+)[\"']\s*\)\s+version\s+[\"'](?P<version>[^\"']+)[\"']",
                text,
            ):
                dependencies.append(
                    DeclaredDependency(f"plugin:{match.group('name')}", match.group("version"), source)
                )
        return dependencies

    def _parse_package_json(self, path: Path) -> list[DeclaredDependency]:
        """Read npm dependency ranges without resolving or installing them."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        dependencies: list[DeclaredDependency] = []
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            values = data.get(section, {})
            if not isinstance(values, dict):
                continue
            for name, constraint in values.items():
                if isinstance(name, str) and isinstance(constraint, str):
                    dependencies.append(DeclaredDependency(name, constraint, f"{path.name} ({section})"))
        return dependencies

    def _parse_gradle_version_catalog(self, path: Path) -> list[DeclaredDependency]:
        """Parse a Gradle version catalog when one is present."""
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        versions = data.get("versions", {})
        libraries = data.get("libraries", {})
        if not isinstance(versions, dict) or not isinstance(libraries, dict):
            return []
        source = path.relative_to(self.workspace).as_posix()
        dependencies: list[DeclaredDependency] = []
        for alias, value in libraries.items():
            if not isinstance(value, dict):
                continue
            module = value.get("module")
            if not isinstance(module, str):
                continue
            version = value.get("version")
            if isinstance(version, dict):
                version = version.get("ref")
            if isinstance(version, str):
                version = versions.get(version, version)
            dependencies.append(
                DeclaredDependency(module, str(version) if version is not None else None, source)
            )
        return dependencies

    @staticmethod
    def _from_gradle_coordinate(
        coordinate: str, source: str, variables: dict[str, str]
    ) -> DeclaredDependency | None:
        parts = coordinate.removesuffix("@aar").split(":", 2)
        if len(parts) < 2:
            return None
        constraint = parts[2] if len(parts) == 3 else None
        if constraint and constraint.startswith("$"):
            constraint = variables.get(constraint.removeprefix("$"), constraint)
        return DeclaredDependency(":".join(parts[:2]), constraint or None, source)

    @staticmethod
    def _deduplicate(found: list[DeclaredDependency]) -> list[DeclaredDependency]:
        grouped: dict[tuple[str, str | None], list[str]] = defaultdict(list)
        for dependency in found:
            grouped[(dependency.name.lower(), dependency.constraint)].append(dependency.source)
        return [
            DeclaredDependency(name, constraint, ", ".join(sorted(sources)))
            for (name, constraint), sources in sorted(grouped.items())
        ]

    def _parse_requirements(self, path: Path) -> list[DeclaredDependency]:
        dependencies: list[DeclaredDependency] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            dependency = self._from_requirement(line, path.name)
            if dependency is not None:
                dependencies.append(dependency)
        return dependencies

    def _parse_pyproject(self, path: Path) -> list[DeclaredDependency]:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        dependencies: list[DeclaredDependency] = []
        project = data.get("project", {})
        if isinstance(project, dict):
            dependencies.extend(self._from_requirement_values(project.get("dependencies"), path.name))
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                for values in optional.values():
                    dependencies.extend(self._from_requirement_values(values, path.name))
        poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
        if isinstance(poetry, dict):
            for section in ("dependencies", "group"):
                values = poetry.get(section, {})
                if section == "group" and isinstance(values, dict):
                    for group in values.values():
                        if isinstance(group, dict):
                            dependencies.extend(self._from_poetry_mapping(group.get("dependencies", {}), path.name))
                elif isinstance(values, dict):
                    dependencies.extend(self._from_poetry_mapping(values, path.name))
        return dependencies

    def _parse_setup_cfg(self, path: Path) -> list[DeclaredDependency]:
        parser = configparser.ConfigParser()
        parser.read_string(path.read_text(encoding="utf-8"))
        if not parser.has_option("options", "install_requires"):
            return []
        return self._from_requirement_values(
            parser.get("options", "install_requires").splitlines(), path.name
        )

    def _parse_setup_py(self, path: Path) -> list[DeclaredDependency]:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            return []
        values: list[object] = []
        assignments: dict[str, object] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = value
            if isinstance(node, ast.Call) and self._call_name(node.func) == "setup":
                for keyword in node.keywords:
                    if keyword.arg != "install_requires":
                        continue
                    try:
                        values.append(ast.literal_eval(keyword.value))
                    except (ValueError, TypeError):
                        if isinstance(keyword.value, ast.Name):
                            values.append(assignments.get(keyword.value.id, []))
        dependencies: list[DeclaredDependency] = []
        for value in values:
            dependencies.extend(self._from_requirement_values(value, path.name))
        return dependencies

    def _parse_pipfile(self, path: Path) -> list[DeclaredDependency]:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        dependencies: list[DeclaredDependency] = []
        for section in ("packages", "dev-packages"):
            values = data.get(section, {})
            if not isinstance(values, dict):
                continue
            for name, value in values.items():
                constraint = value.get("version") if isinstance(value, dict) else value
                dependencies.append(
                    DeclaredDependency(name, None if constraint in (None, "*") else str(constraint), path.name)
                )
        return dependencies

    def _from_requirement_values(self, values: object, source: str) -> list[DeclaredDependency]:
        if not isinstance(values, list | tuple):
            return []
        return [dependency for value in values if (dependency := self._from_requirement(str(value), source))]

    @staticmethod
    def _from_poetry_mapping(values: object, source: str) -> list[DeclaredDependency]:
        if not isinstance(values, dict):
            return []
        return [
            DeclaredDependency(name, None if version in ("*", None) else str(version), source)
            for name, value in values.items()
            if name.lower() != "python"
            for version in [value.get("version") if isinstance(value, dict) else value]
        ]

    @staticmethod
    def _from_requirement(raw: str, source: str) -> DeclaredDependency | None:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(("-r", "-c", "--")):
            return None
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            return None
        constraint = str(requirement.specifier) or None
        return DeclaredDependency(requirement.name, constraint, source)

    @staticmethod
    def _call_name(node: ast.expr) -> str | None:
        return node.id if isinstance(node, ast.Name) else None

