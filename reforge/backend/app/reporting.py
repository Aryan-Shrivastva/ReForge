"""Deterministic final-report generation for completed modernization runs."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.state import ProjectState, TestReport
from .tools.ast_tools import RuleCatalog, scan_android_deprecated_patterns, scan_deprecated_patterns


@dataclass(frozen=True, slots=True)
class ReportArtifacts:
    markdown: Path
    json: Path


class ReportGenerator:
    """Produce auditable Markdown and JSON from only persisted run data and Git facts."""

    def __init__(self, catalog: RuleCatalog) -> None:
        self.catalog = catalog

    def write(self, state: ProjectState) -> ReportArtifacts:
        workspace = Path(state.workspace_path)
        payload = self.build(state, workspace)
        report_root = workspace.parent
        report_root.mkdir(parents=True, exist_ok=True)
        markdown = report_root / "report.md"
        json_path = report_root / "report.json"
        markdown.write_text(self.render_markdown(payload), encoding="utf-8")
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return ReportArtifacts(markdown=markdown, json=json_path)

    def build(self, state: ProjectState, workspace: Path) -> dict[str, Any]:
        initial_patterns = state.analysis.deprecated_patterns if state.analysis else []
        remaining_patterns = (
            [
                *scan_deprecated_patterns(workspace, self.catalog),
                *scan_android_deprecated_patterns(workspace),
            ]
            if workspace.is_dir()
            else []
        )
        remaining_keys = {(item.rule_id, item.file, item.line) for item in remaining_patterns}
        fixed_patterns = [
            item
            for item in initial_patterns
            if (item.rule_id, item.file, item.line) not in remaining_keys
        ]
        latest = state.test_runs[-1] if state.test_runs else None
        changes = self._git_changes(workspace, state.base_commit)
        unresolved = self._unresolved_issues(state, remaining_patterns, latest)
        return {
            "run_id": state.run_id,
            "objective": state.objective,
            "objective_origin": state.objective_origin,
            "outcome": {
                "status": state.final_status.value if state.final_status else "INCOMPLETE",
                "stage": state.stage.value,
            },
            "dependencies": [item.model_dump(mode="json") for item in (state.analysis.dependencies if state.analysis else [])],
            "deprecated_patterns": {
                "found": [item.model_dump(mode="json") for item in initial_patterns],
                "fixed": [item.model_dump(mode="json") for item in fixed_patterns],
                "remaining": [item.model_dump(mode="json") for item in remaining_patterns],
            },
            "plan": state.plan.model_dump(mode="json") if state.plan else None,
            "changes": changes,
            "tests": {
                "baseline": self._test_summary(state.baseline),
                "final": self._test_summary(latest),
                "baseline_pass_set": state.baseline_pass_set,
            },
            "repairs": [item.model_dump(mode="json") for item in state.repairs],
            "replans": state.replans,
            "metrics": state.metrics.model_dump(mode="json"),
            "unresolved_issues": unresolved,
            "manual_follow_ups": self._follow_ups(state, unresolved),
            "reproduction": {
                "repo_source": state.repo_source,
                "workspace": state.workspace_path,
                "base_commit": state.base_commit,
                "best_commit": state.best_commit,
                "work_branch": state.work_branch,
                "budgets": state.budgets.model_dump(mode="json"),
            },
        }

    @staticmethod
    def _test_summary(report: TestReport | None) -> dict[str, Any] | None:
        if report is None:
            return None
        return {
            "phase": report.phase,
            "install_ok": report.install_ok,
            "passed": len(report.passed),
            "failed": len(report.failed),
            "errored": len(report.errored),
            "skipped": len(report.skipped),
            "duration_s": report.duration_s,
            "lint_issues": report.lint_issues,
            "typecheck_errors": report.typecheck_errors,
            "generated_tests": report.generated_tests,
        }

    @staticmethod
    def _git_changes(workspace: Path, base_commit: str) -> dict[str, Any]:
        if not workspace.is_dir() or not base_commit:
            return {"files": [], "files_changed": 0, "lines_added": 0, "lines_removed": 0}
        command = ["git", "-C", str(workspace), "diff", "--numstat", base_commit]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            return {"files": [], "files_changed": 0, "lines_added": 0, "lines_removed": 0}
        files: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, path = parts
            files.append({"path": path, "lines_added": int(added) if added.isdigit() else 0, "lines_removed": int(removed) if removed.isdigit() else 0})
        return {
            "files": files,
            "files_changed": len(files),
            "lines_added": sum(item["lines_added"] for item in files),
            "lines_removed": sum(item["lines_removed"] for item in files),
        }

    @staticmethod
    def _unresolved_issues(state: ProjectState, remaining: list[Any], latest: TestReport | None) -> list[str]:
        issues = [f"Deprecated pattern remains: {item.rule_id} at {item.file}:{item.line}" for item in remaining]
        if latest and (latest.failed or latest.errored):
            issues.append(f"Final verification has {len(latest.failed) + len(latest.errored)} failing/error test(s).")
        if state.plan:
            issues.extend(f"Plan step {step.id} was {step.status}." for step in state.plan.steps if step.status != "applied")
        if state.final_status and state.final_status.value != "SUCCESS":
            issues.append(f"Run finished with {state.final_status.value}; review the trace and repair history.")
        return issues

    @staticmethod
    def _follow_ups(state: ProjectState, unresolved: list[str]) -> list[str]:
        follow_ups = ["Review the generated diff and commit history before merging."]
        if state.baseline and state.baseline.generated_tests:
            follow_ups.append("Review generated characterization tests before treating them as permanent product tests.")
        if unresolved:
            follow_ups.append("Resolve each unresolved issue, then re-run ReForge from the recorded base commit.")
        return follow_ups

    @staticmethod
    def render_markdown(payload: dict[str, Any]) -> str:
        outcome = payload["outcome"]
        dependencies = payload["dependencies"]
        patterns = payload["deprecated_patterns"]
        changes = payload["changes"]
        tests = payload["tests"]
        lines = [
            "# ReForge modernization report",
            "",
            f"**Run:** `{payload['run_id']}`  ",
            f"**Status:** `{outcome['status']}`  ",
            f"**Objective:** {payload['objective']}",
            f"**Objective origin:** `{payload['objective_origin']}`",
            "",
            "## Dependencies",
            "",
            "| Dependency | Before | Target | Status | Reason |",
            "|---|---:|---:|---|---|",
        ]
        if dependencies:
            lines.extend(
                f"| {item['name']} | {item['current'] or '—'} | {item['target'] or '—'} | {item['status']} | {item['reason']} |"
                for item in dependencies
            )
        else:
            lines.append("| — | — | — | — | No dependency findings |")
        lines.extend([
            "",
            "## Deprecated patterns",
            "",
            f"Found: **{len(patterns['found'])}** · Fixed: **{len(patterns['fixed'])}** · Remaining: **{len(patterns['remaining'])}**",
            "",
        ])
        for item in patterns["remaining"]:
            lines.append(f"- `{item['rule_id']}` — `{item['file']}:{item['line']}` — {item['replacement_hint']}")
        if not patterns["remaining"]:
            lines.append("- No catalogued deprecated patterns remain.")
        lines.extend(["", "## Plan"])
        if payload["plan"]:
            lines.extend(["", "| Step | Status | Commit | Description |", "|---|---|---|---|"])
            lines.extend(
                f"| {step['id']} | {step['status']} | {step['commit_sha'] or '—'} | {step['description']} |"
                for step in payload["plan"]["steps"]
            )
        else:
            lines.extend(["", "No validated plan was produced."])
        lines.extend([
            "",
            "## Changed files",
            "",
            f"{changes['files_changed']} file(s), +{changes['lines_added']} / -{changes['lines_removed']} lines.",
            "",
        ])
        if changes["files"]:
            lines.extend(f"- `{item['path']}`: +{item['lines_added']} / -{item['lines_removed']}" for item in changes["files"])
        else:
            lines.append("- No Git diff available.")
        lines.extend(["", "## Tests", ""])
        for label in ("baseline", "final"):
            report = tests[label]
            lines.append(f"- **{label.title()}:** {report if report is not None else 'not available'}")
        lines.extend(["", "## Repairs and replans", ""])
        if payload["repairs"]:
            lines.extend(
                f"- R{item['iteration']}: {item['diagnosis']} ({item['failing_before']} → {item['failing_after'] if item['failing_after'] is not None else 'pending'})"
                for item in payload["repairs"]
            )
        else:
            lines.append("- No repair attempts.")
        lines.append(f"- Replans: {payload['replans']}")
        lines.extend(["", "## Metrics", "", "```json", json.dumps(payload["metrics"], indent=2, sort_keys=True), "```"])
        lines.extend(["", "## Unresolved issues", ""])
        if payload["unresolved_issues"]:
            lines.extend(f"- {item}" for item in payload["unresolved_issues"])
        else:
            lines.append("- None.")
        lines.extend(["", "## Manual follow-ups", ""])
        lines.extend(f"- {item}" for item in payload["manual_follow_ups"])
        reproduction = payload["reproduction"]
        lines.extend([
            "",
            "## Reproduction",
            "",
            f"- Source: `{reproduction['repo_source']}`",
            f"- Base commit: `{reproduction['base_commit']}`",
            f"- Best commit: `{reproduction['best_commit'] or '—'}`",
            f"- Working branch: `{reproduction['work_branch']}`",
            f"- Budgets: `{json.dumps(reproduction['budgets'], sort_keys=True)}`",
            "",
        ])
        return "\n".join(lines)
