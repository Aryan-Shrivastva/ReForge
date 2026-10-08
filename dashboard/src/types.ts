export type FinalStatus = "SUCCESS" | "PARTIAL" | "FAILED" | "ABORTED";

export interface TraceEvent {
  ts: string;
  stage: string;
  agent: string;
  kind: "start" | "tool_call" | "result" | "decision" | "error";
  summary: string;
  data: Record<string, unknown>;
}

export interface Failure {
  test_id: string;
  error_type: string;
  message: string;
  traceback_excerpt: string;
  file: string | null;
}

export interface TestReport {
  phase: "baseline" | "post_transform" | "post_repair";
  install_ok: boolean;
  passed: string[];
  failed: Failure[];
  errored: Failure[];
  skipped: string[];
  duration_s: number;
  lint_issues: number | null;
  typecheck_errors: number | null;
  generated_tests: boolean;
}

export interface ProjectState {
  run_id: string;
  repo_source: string;
  objective: string;
  workspace_path: string;
  work_branch: string;
  base_commit: string;
  stage: string;
  analysis: AnalysisReport | null;
  plan: Plan | null;
  baseline: TestReport | null;
  baseline_pass_set: string[];
  protected_test_files: string[];
  test_runs: TestReport[];
  repairs: RepairAttempt[];
  replans: number;
  budgets: Record<string, number>;
  metrics: Metrics;
  trace: TraceEvent[];
  final_status: FinalStatus | null;
  report_path: string | null;
}

export interface AnalysisReport {
  dependencies: DependencyFinding[];
  deprecated_patterns: DeprecatedPattern[];
  risks: string[];
  summary: string;
}

export interface DependencyFinding {
  name: string;
  current: string | null;
  target: string | null;
  status: string;
  reason: string;
}

export interface DeprecatedPattern {
  rule_id: string;
  file: string;
  line: number;
  snippet: string;
  replacement_hint: string;
}

export interface Plan {
  version: number;
  rationale: string;
  steps: PlanStep[];
}

export interface PlanStep {
  id: string;
  title: string;
  description: string;
  files: string[];
  depends_on: string[];
  kind: string;
  validation_hint: string;
  status: "pending" | "applied" | "failed" | "skipped";
  commit_sha: string | null;
}

export interface RepairAttempt {
  iteration: number;
  diagnosis: string;
  root_cause_category: string;
  files_changed: string[];
  commit_sha: string | null;
  failing_before: number;
  failing_after: number | null;
}

export interface Metrics {
  tokens_in: number;
  tokens_out: number;
  agent_calls: Record<string, number>;
  wall_clock_s: number;
  files_changed: number;
  lines_added: number;
  lines_removed: number;
}

export interface Benchmark {
  name: string;
  objective: string;
  has_tests: boolean;
  success: string;
}

export interface EvaluationSummary {
  artifact_path: string | null;
  success_rate: number;
  partial_rate: number;
  runs: Array<{
    benchmark: string;
    status: FinalStatus | null;
    behavior_preservation: number;
    repair_iterations: number;
    replans: number;
    regressions_introduced: number;
    wall_clock_s: number;
  }>;
}
