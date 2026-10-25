"""AST-based scans against the versioned ReForge deprecation rule catalog."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re

import yaml

from ..core.state import DeprecatedPattern
from .languages import JS_LIKE_EXTENSIONS, iter_source_files

_RULES_DIRECTORY = Path(__file__).resolve().parents[1] / "rules"


@dataclass(frozen=True, slots=True)
class DeprecationRule:
    id: str
    kind: str
    module: str | None
    name: str
    replacement_hint: str


class RuleCatalog:
    def __init__(self, rules: list[DeprecationRule]) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path) -> "RuleCatalog":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise ValueError("rule catalog must contain a 'rules' list")
        return cls(
            [
                DeprecationRule(
                    id=str(item["id"]),
                    kind=str(item["kind"]),
                    module=str(item["module"]) if item.get("module") else None,
                    name=str(item["name"]),
                    replacement_hint=str(item["replacement_hint"]),
                )
                for item in data["rules"]
                if isinstance(item, dict)
            ]
        )

    def lookup(self, rule_id: str) -> DeprecationRule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)


class DeprecatedPatternScanner(ast.NodeVisitor):
    """Find syntactic deprecations without importing or executing project code."""

    def __init__(self, catalog: RuleCatalog, relative_file: str, source: str) -> None:
        self.catalog = catalog
        self.relative_file = relative_file
        self.lines = source.splitlines()
        self.findings: list[DeprecatedPattern] = []
        self._seen: set[tuple[str, int]] = set()

    def scan(self) -> list[DeprecatedPattern]:
        self.visit(ast.parse("\n".join(self.lines), filename=self.relative_file))
        return self.findings

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._match("import", None, alias.name.split(".", 1)[0], node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self._match("from_import", node.module, alias.name, node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._match("attribute", self._attribute_root(node), node.attr, node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._match("name", None, node.id, node.lineno)
        self.generic_visit(node)

    def _match(self, kind: str, module: str | None, name: str | None, line: int) -> None:
        for rule in self.catalog.rules:
            if rule.kind != kind or rule.name != name:
                continue
            if rule.module is not None and rule.module != module:
                continue
            key = (rule.id, line)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.findings.append(
                DeprecatedPattern(
                    rule_id=rule.id,
                    file=self.relative_file,
                    line=line,
                    snippet=self.lines[line - 1].strip(),
                    replacement_hint=rule.replacement_hint,
                    source="ast_rule",
                )
            )

    @staticmethod
    def _attribute_root(node: ast.Attribute) -> str | None:
        current: ast.expr = node
        while isinstance(current, ast.Attribute):
            current = current.value
        return current.id if isinstance(current, ast.Name) else None


def scan_deprecated_patterns(workspace: Path, catalog: RuleCatalog) -> list[DeprecatedPattern]:
    findings: list[DeprecatedPattern] = []
    root = workspace.resolve()
    for path in iter_source_files(root, (".py",)):
        try:
            source = path.read_text(encoding="utf-8")
            scanner = DeprecatedPatternScanner(catalog, path.relative_to(root).as_posix(), source)
            findings.extend(scanner.scan())
        except (UnicodeDecodeError, SyntaxError, ValueError):
            continue
    return findings


@dataclass(frozen=True, slots=True)
class TextRule:
    """A per-line regular-expression rule for languages ReForge does not parse."""

    id: str
    pattern: re.Pattern[str]
    replacement_hint: str
    extensions: tuple[str, ...] = JS_LIKE_EXTENSIONS


class TextRuleCatalog:
    def __init__(self, rules: list[TextRule]) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path) -> "TextRuleCatalog":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise ValueError("text rule catalog must contain a 'rules' list")
        rules: list[TextRule] = []
        for item in data["rules"]:
            if not isinstance(item, dict):
                continue
            extensions = item.get("extensions")
            rules.append(
                TextRule(
                    id=str(item["id"]),
                    pattern=re.compile(str(item["pattern"])),
                    replacement_hint=str(item["replacement_hint"]),
                    extensions=tuple(str(value) for value in extensions) if extensions else JS_LIKE_EXTENSIONS,
                )
            )
        return cls(rules)

    def lookup(self, rule_id: str) -> TextRule:
        for rule in self.rules:
            if rule.id == rule_id:
                return rule
        raise KeyError(rule_id)


@lru_cache(maxsize=1)
def default_javascript_catalog() -> TextRuleCatalog:
    return TextRuleCatalog.from_yaml(_RULES_DIRECTORY / "javascript_deprecations.yaml")


_MINIFIED_LINE_LENGTH = 1_000


def scan_text_rules(workspace: Path, catalog: TextRuleCatalog) -> list[DeprecatedPattern]:
    """Apply line rules to JavaScript/TypeScript sources without executing or bundling them.

    Minified bundles (very long lines, ``*.min.js``) are skipped: they are
    build output, not maintainable source.
    """
    root = workspace.resolve()
    extensions = tuple(sorted({extension for rule in catalog.rules for extension in rule.extensions}))
    findings: list[DeprecatedPattern] = []
    for path in iter_source_files(root, extensions):
        relative = path.relative_to(root).as_posix()
        if ".min." in PurePosixPath(relative).name:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        suffix = path.suffix.lower()
        applicable = [rule for rule in catalog.rules if suffix in rule.extensions]
        for line_number, line in enumerate(lines, start=1):
            if len(line) > _MINIFIED_LINE_LENGTH or line.lstrip().startswith(("//", "*", "/*")):
                continue
            for rule in applicable:
                if rule.pattern.search(line):
                    findings.append(
                        DeprecatedPattern(
                            rule_id=rule.id,
                            file=relative,
                            line=line_number,
                            snippet=line.strip()[:300],
                            replacement_hint=rule.replacement_hint,
                            source="ast_rule",
                        )
                    )
    return findings


def scan_all_deprecated_patterns(
    workspace: Path,
    catalog: RuleCatalog,
    text_catalog: TextRuleCatalog | None = None,
) -> list[DeprecatedPattern]:
    """Run every deterministic scanner: Python AST, JavaScript/TypeScript text, and Android rules."""
    root = workspace.resolve()
    return [
        *scan_deprecated_patterns(root, catalog),
        *scan_text_rules(root, text_catalog or default_javascript_catalog()),
        *scan_android_deprecated_patterns(root),
    ]


_ANDROID_TEXT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ANDROID-ACTIVITY-RESULTS",
        re.compile(r"\bstartActivityForResult\s*\("),
        "Use AndroidX Activity Result APIs (registerForActivityResult) instead.",
    ),
    (
        "ANDROID-ACTIVITY-RESULTS",
        re.compile(r"\bonActivityResult\s*\("),
        "Migrate the result handling to an AndroidX Activity Result callback.",
    ),
)


def scan_android_deprecated_patterns(workspace: Path) -> list[DeprecatedPattern]:
    """Find a small, explicit set of Android API migrations without compilation.

    These are text rules rather than Python AST rules because the repository may
    contain Java or Kotlin.  They are intentionally narrow: ReForge reports
    only patterns it can point to in source, rather than inferring staleness
    from the age of a repository.
    """
    findings: list[DeprecatedPattern] = []
    root = workspace.resolve()
    for path in iter_source_files(root, (".java", ".kt")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root).as_posix()
        for line_number, line in enumerate(lines, start=1):
            for rule_id, pattern, hint in _ANDROID_TEXT_RULES:
                if pattern.search(line):
                    findings.append(
                        DeprecatedPattern(
                            rule_id=rule_id,
                            file=relative,
                            line=line_number,
                            snippet=line.strip(),
                            replacement_hint=hint,
                            source="ast_rule",
                        )
                    )
    return findings
