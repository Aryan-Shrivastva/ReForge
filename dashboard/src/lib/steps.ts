import type { ProjectState, TestReport } from "../types";
import { runStatus } from "./format";

export type StepKey = "ANALYZE" | "PLAN" | "BASELINE" | "APPROVE" | "TRANSFORM" | "TEST" | "REPORT";
export type StepStatus = "done" | "failed" | "warning" | "running" | "waiting" | "pending" | "skipped";

export interface Step {
  key: StepKey;
  label: string;
  status: StepStatus;
  seconds: number | null;
  summary: string;
}

const ALL_STEPS: StepKey[] = ["ANALYZE", "PLAN", "BASELINE", "APPROVE", "TRANSFORM", "TEST", "REPORT"];
const LABELS: Record<StepKey, string> = {
  ANALYZE: "Analyze repository",
  PLAN: "Plan changes",
  BASELINE: "Record baseline",
  APPROVE: "Approve plan",
  TRANSFORM: "Apply changes",
  TEST: "Verify behavior",
  REPORT: "Write report",
};
/** REPAIR and REPLAN are loops inside verification. */
const STAGE_TO_STEP: Record<string, StepKey> = {
  ANALYZE: "ANALYZE", PLAN: "PLAN", BASELINE: "BASELINE", AWAIT_APPROVAL: "APPROVE", TRANSFORM: "TRANSFORM",
  TEST: "TEST", REPAIR: "TEST", REPLAN: "TEST", REPORT: "REPORT", DONE: "REPORT",
};

/** Derive the six user-facing steps, their status, and timing from the trace. */
export function buildSteps(state: ProjectState): Step[] {
  // The approval step exists only for runs that require a human decision.
  const ORDER = ALL_STEPS.filter((key) => key !== "APPROVE" || state.requires_approval || (state.reviews ?? []).length > 0);
  const entered = new Map<StepKey, number>();
  let furthest = -1;
  for (const event of state.trace) {
    const to = typeof event.data.to === "string" ? event.data.to : null;
    if (!to) continue;
    const key = STAGE_TO_STEP[to];
    if (!key) continue;
    if (!entered.has(key)) entered.set(key, new Date(event.ts).getTime());
    if (key !== "REPORT") furthest = Math.max(furthest, ORDER.indexOf(key));
  }
  const end = state.trace.length ? new Date(state.trace[state.trace.length - 1].ts).getTime() : null;
  const status = runStatus(state);
  const finished = Boolean(state.final_status);
  const current = finished ? -1 : ORDER.indexOf(STAGE_TO_STEP[state.stage] ?? "ANALYZE");
  const stoppedAt = finished && status !== "SUCCESS" ? Math.max(furthest, 0) : -1;

  return ORDER.map((key, index) => {
    let stepStatus: StepStatus = "pending";
    if (key === "REPORT") {
      stepStatus = state.report_path ? "done" : finished ? "skipped" : "pending";
    } else if (finished) {
      // A successful run passed every step by definition, whatever the trace recorded.
      if (status === "SUCCESS" || index < stoppedAt) stepStatus = "done";
      else if (index === stoppedAt) stepStatus = status === "PARTIAL" ? "warning" : "failed";
      else stepStatus = "skipped";
    } else if (index < current) {
      stepStatus = "done";
    } else if (index === current) {
      stepStatus = key === "APPROVE" ? "waiting" : "running";
    }
    const start = entered.get(key);
    const nextStart = ORDER.slice(index + 1).map((next) => entered.get(next)).find((value) => value !== undefined);
    const stop = nextStart ?? (stepStatus === "running" ? Date.now() : end);
    const seconds = start !== undefined && stop !== null && stepStatus !== "pending" && stepStatus !== "skipped"
      ? Math.max((stop - start) / 1000, 0)
      : null;
    return { key, label: LABELS[key], status: stepStatus, seconds, summary: stepSummary(state, key, stepStatus) };
  });
}

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

function stepSummary(state: ProjectState, key: StepKey, status: StepStatus): string {
  if (status === "pending") return "Waiting";
  if (status === "skipped") return "Not reached";
  if (status === "waiting") return "Waiting for your approval";
  if (status === "running") return state.trace.at(-1)?.summary ?? "In progress";
  const analysis = state.analysis;
  const plan = state.plan;
  const baselineAttempt = state.baseline ?? state.test_runs.find((report) => report.phase === "baseline");
  const latest = state.test_runs.at(-1);
  switch (key) {
    case "ANALYZE":
      if (!analysis) return "Analysis did not complete";
      return [
        analysis.languages?.length ? analysis.languages.map(languageName).join(", ") : null,
        plural(analysis.dependencies.length, "dependency").replace("dependencys", "dependencies"),
        plural(analysis.deprecated_patterns.length, "deprecated pattern"),
      ].filter(Boolean).join(" · ");
    case "PLAN":
      return plan ? plural(plan.steps.length, "change") + " planned" : "No plan was produced";
    case "BASELINE":
      return baselineSummary(state, baselineAttempt);
    case "APPROVE": {
      const review = (state.reviews ?? []).at(-1);
      const revisions = (state.reviews ?? []).filter((item) => item.decision === "revision_requested").length;
      const revised = revisions ? ` after ${plural(revisions, "revision")}` : "";
      if (review?.decision === "rejected") return "You rejected the plan";
      if (review?.decision === "approved") {
        const total = review.approved_steps.length + review.declined_steps.length;
        return `Approved ${review.approved_steps.length} of ${plural(total, "change")}${revised}`;
      }
      return "Not approved";
    }
    case "TRANSFORM": {
      if (!plan) return "Nothing to apply";
      const applied = plan.steps.filter((step) => step.status === "applied").length;
      const approved = plan.steps.filter((step) => step.status !== "declined").length;
      const files = state.metrics.files_changed;
      return `${applied} of ${plural(approved, "approved change")} applied` + (files ? ` · ${plural(files, "file")} (+${state.metrics.lines_added} −${state.metrics.lines_removed})` : "");
    }
    case "TEST": {
      if (!latest || latest === state.baseline) return "Not run";
      const kept = state.baseline_pass_set.filter((id) => latest.passed.includes(id)).length;
      const repairs = state.repairs.length ? ` after ${plural(state.repairs.length, "repair")}` : "";
      return `${kept} of ${state.baseline_pass_set.length} baseline ${state.smoke_baseline ? "checks" : "tests"} pass${repairs}`;
    }
    case "REPORT":
      return "Markdown and JSON report ready";
  }
}

function baselineSummary(state: ProjectState, report: TestReport | undefined): string {
  if (!report) return "Not run";
  if (!report.install_ok) return "Dependencies failed to install";
  if (state.smoke_baseline) return `No tests found · ${plural(state.baseline_pass_set.length, "syntax check")} recorded`;
  if (!state.baseline_pass_set.length) return "No runnable tests found";
  const generated = report.generated_tests ? " (includes generated tests)" : "";
  return `${plural(state.baseline_pass_set.length, "passing test")} frozen${generated}`;
}

export function languageName(value: string): string {
  return { javascript: "JavaScript", typescript: "TypeScript", python: "Python", java: "Java", kotlin: "Kotlin" }[value] ?? value;
}

export interface Explanation {
  headline: string;
  detail: string;
  nextSteps: string[];
}

/** Plain-language account of the outcome, written for the person who started the run. */
export function explain(state: ProjectState): Explanation {
  const status = runStatus(state);
  const baselineAttempt = state.baseline ?? state.test_runs.find((report) => report.phase === "baseline");
  const plan = state.plan;
  const applied = plan?.steps.filter((step) => step.status === "applied").length ?? 0;
  const lastError = [...state.trace].reverse().find((event) => event.kind === "error")?.summary;

  if (status === "AWAITING") {
    const tests = state.smoke_baseline ? "syntax checks" : "tests";
    return {
      headline: "Your approval is needed",
      detail: `ReForge analyzed the repository, recorded ${plural(state.baseline_pass_set.length, `baseline ${tests.replace(/s$/, "")}`)}, and proposes ${plural(plan?.steps.length ?? 0, "change")}. Nothing has been changed yet, and nothing will be until you decide.`,
      nextSteps: ["Review the proposed changes below.", "Approve all or some of them, ask for changes, or reject the plan."],
    };
  }
  if (status === "RUNNING" || status === "QUEUED") {
    return { headline: "Run in progress", detail: state.trace.at(-1)?.summary ?? "Waiting to start.", nextSteps: [] };
  }
  if (status === "SUCCESS") {
    const checks = state.smoke_baseline ? "syntax checks" : "baseline tests";
    return {
      headline: `${plural(applied, "change")} applied safely`,
      detail: `Every one of the ${state.baseline_pass_set.length} ${checks} recorded before the changes still passes${state.repairs.length ? `, after ${plural(state.repairs.length, "automatic repair")}` : ""}.`
        + (state.smoke_baseline ? " This repository has no tests, so behavior itself was not verified." : ""),
      nextSteps: ["Review the changes, then merge the work branch.", ...(state.smoke_baseline ? ["Add tests so future runs can verify behavior, not just syntax."] : [])],
    };
  }
  if (status === "ABORTED") {
    const review = (state.reviews ?? []).at(-1);
    if (review?.decision === "rejected") {
      return {
        headline: "Plan rejected",
        detail: `You rejected the proposed plan, so no code was changed.${review.feedback ? ` Your note: "${review.feedback}"` : ""}`,
        nextSteps: ["Rerun the project to get a new proposal."],
      };
    }
    return { headline: "Run cancelled", detail: "The run was stopped before it finished. Nothing was merged.", nextSteps: ["Start a new run when ready."] };
  }
  if (baselineAttempt && !baselineAttempt.install_ok) {
    return {
      headline: "Dependencies could not be installed",
      detail: "ReForge installs the project in an isolated sandbox before changing anything. The install failed, so no changes were made.",
      nextSteps: [
        "Open the Record baseline step to read the install log.",
        "Check that the lockfile matches package.json / requirements and that the project installs on a clean machine.",
      ],
    };
  }
  if (baselineAttempt && !state.baseline_pass_set.length) {
    return {
      headline: "No tests to protect the changes",
      detail: "ReForge only changes code it can verify. This repository has no runnable tests, generated tests did not pass, and no source file could be syntax-checked.",
      nextSteps: ["Add a test script (for example with node:test, Jest, or pytest) and run again."],
    };
  }
  if (status === "PARTIAL") {
    const failedStep = plan?.steps.find((step) => step.status === "failed");
    const reason = failedStep ? transformationError(state, failedStep.id) : null;
    const blocked = plan?.steps.filter((step) => step.status === "skipped").length ?? 0;
    const why = failedStep
      ? ` "${failedStep.title}" was rejected${reason ? `: ${reason}` : ""}.${blocked ? ` ${plural(blocked, "later change")} depended on it and ${blocked === 1 ? "was" : "were"} skipped.` : ""}`
      : "";
    return {
      headline: applied ? `${applied} of ${plural(plan?.steps.length ?? 0, "change")} applied` : "No change could be applied",
      detail: (applied
        ? "The applied changes keep every baseline check passing, but some planned changes could not be applied safely."
        : "The code is unchanged.") + why,
      nextSteps: applied
        ? ["Review the applied changes before merging.", "Open Apply changes to see why the others were not applied."]
        : ["Rerun the project to let the agents plan again.", "Open Apply changes for each change's details."],
    };
  }
  return {
    headline: "Run failed",
    detail: lastError ?? "The run stopped before completing.",
    nextSteps: ["Open the failed step below for details.", "The Logs tab has the full event history."],
  };
}

function transformationError(state: ProjectState, stepId: string): string | null {
  const event = [...state.trace].reverse().find((item) => item.agent === "transformation" && item.summary.startsWith(`${stepId} `));
  return typeof event?.data.error === "string" ? event.data.error : null;
}
