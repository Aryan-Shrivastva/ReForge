from pathlib import Path

from reforge.backend.app.core.state import (
    AnalysisReport,
    DeprecatedPattern,
    FinalStatus,
    TestInventory as StateTestInventory,
    TestReport as StateTestReport,
)
from reforge.backend.app.evaluation import Ablation, BenchmarkCatalog, BenchmarkRunner, evaluate_run, summarize_runs
from reforge.backend.app.tools.ast_tools import RuleCatalog, scan_all_deprecated_patterns
from reforge.backend.tests.conftest import make_state


def test_catalog_discovers_the_required_controlled_coverage_cases() -> None:
    definitions = BenchmarkCatalog(Path("benchmarks")).list()

    assert len(definitions) == 6
    assert {definition.name for definition in definitions} >= {
        "node-legacy-app",
        "flask-legacy-app",
        "fastapi-pydantic-v1",
        "stdlib-deprecations",
        "setup-py-migration",
        "no-tests-characterization",
    }
    assert BenchmarkCatalog(Path("benchmarks")).get("no-tests-characterization").has_tests is False
    rules = RuleCatalog.from_yaml(Path("reforge/backend/app/rules/python_deprecations.yaml"))
    for definition in definitions:
        observed = {
            (pattern.rule_id, pattern.file, pattern.line)
            for pattern in scan_all_deprecated_patterns(definition.repository_path, rules)
        }
        expected = {
            (pattern.rule_id, pattern.file, pattern.line)
            for pattern in definition.ground_truth.deprecated_patterns
        }
        assert observed == expected


def test_evaluation_calculates_behavior_detection_regressions_and_aggregates() -> None:
    benchmark = BenchmarkCatalog(Path("benchmarks")).get("flask-legacy-app")
    state = make_state(run_id="evaluation-run", final_status=FinalStatus.SUCCESS)
    state.analysis = AnalysisReport(
        tests=StateTestInventory(),
        deprecated_patterns=[
            DeprecatedPattern(
                rule_id="flask-escape",
                file="app.py",
                line=1,
                snippet="from flask import escape",
                replacement_hint="Use markupsafe.",
                source="ast_rule",
            )
        ],
        summary="fixture",
    )
    state.baseline_pass_set = ["tests/test_app.py::test_greeting", "tests/test_app.py::test_other"]
    state.test_runs = [
        StateTestReport(
            phase="post_transform",
            install_ok=True,
            passed=["tests/test_app.py::test_greeting", "tests/test_new.py::test_regression"],
            duration_s=0.1,
        )
    ]
    state.metrics.files_changed = 2
    state.metrics.wall_clock_s = 12.5

    result = evaluate_run(benchmark, state)
    summary = summarize_runs([result])

    assert result.behavior_preservation == 0.5
    assert result.detection_precision == 1
    assert result.detection_recall == 1
    assert result.regressions_introduced == 1
    assert summary.success_rate == 1
    assert summary.behavior_preservation is not None and summary.behavior_preservation.mean == 0.5


class FakeWorkflow:
    def __init__(self) -> None:
        self.enable_codemods = True

    def initialize(self, **kwargs: object):
        return make_state(
            run_id=kwargs["run_id"],  # type: ignore[arg-type]
            repo_source=kwargs["repo_source"],  # type: ignore[arg-type]
            objective=kwargs["objective"],  # type: ignore[arg-type]
            workspace_path=str(kwargs["workspace"]),
            budgets=kwargs["budgets"],  # type: ignore[arg-type]
        )

    def run(self, state):
        state.final_status = FinalStatus.SUCCESS
        state.baseline_pass_set = ["tests/test_case.py::test_behavior"]
        state.test_runs = [
            StateTestReport(
                phase="post_transform", install_ok=True, duration_s=0,
                passed=["tests/test_case.py::test_behavior"],
            )
        ]
        return state


def test_benchmark_runner_applies_repetitions_and_ablation_configuration(monkeypatch, tmp_path: Path) -> None:
    runner = BenchmarkRunner(
        BenchmarkCatalog(Path("benchmarks")),
        workflow_factory=lambda _root: FakeWorkflow(),  # type: ignore[arg-type]
        runs_root=tmp_path / "benchmark-runs",
    )
    monkeypatch.setattr(runner, "_materialize", lambda _source, _workspace: None)

    summary = runner.run_suite(
        names=["flask-legacy-app"],
        repetitions=2,
        ablation=Ablation(name="single", description="fixture", max_repairs=0, enable_codemods=False),
    )

    assert len(summary.runs) == 2
    assert summary.success_rate == 1
    assert summary.artifact_path is not None
    assert Path(summary.artifact_path).is_file()
