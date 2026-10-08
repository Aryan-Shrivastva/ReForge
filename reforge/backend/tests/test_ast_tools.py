from pathlib import Path

from reforge.backend.app.tools.ast_tools import DeprecatedPatternScanner, RuleCatalog


def test_scans_known_deprecated_syntax() -> None:
    catalog = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))
    source = """from flask import escape
from collections import Mapping
import imp
import asyncio
loop = asyncio.get_event_loop()
payload = model.dict()
"""

    findings = DeprecatedPatternScanner(catalog, "legacy.py", source).scan()

    assert {finding.rule_id for finding in findings} == {
        "flask-escape",
        "collections-mapping",
        "imp-module",
        "asyncio-get-event-loop",
        "pydantic-dict",
    }
    assert all(finding.file == "legacy.py" for finding in findings)
