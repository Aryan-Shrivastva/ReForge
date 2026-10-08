"""Dependency discovery from common Python project metadata without execution."""

from __future__ import annotations

import ast
import configparser
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
    """Parse declarations only; dependency resolution happens in the sandbox."""

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
        return self._deduplicate(found)

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

