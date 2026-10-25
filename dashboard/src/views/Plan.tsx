import type { PlanStep, ProjectState } from "../types";
import { label, titleCase } from "../lib/format";
import { Callout, Panel, Tag, Waiting } from "../components/ui";

const STEP_TONE: Record<PlanStep["status"], "good" | "bad" | "warn" | "neutral"> = {
  applied: "good",
  failed: "bad",
  skipped: "warn",
  pending: "neutral",
};

export function Plan({ state }: { state: ProjectState }) {
  const plan = state.plan;
  if (!plan) return <Panel title="Plan"><Waiting what="The modernization plan" /></Panel>;
  const baselineAttempt = state.test_runs.find((report) => report.phase === "baseline");
  const blockedByBaseline = !state.baseline && Boolean(baselineAttempt);

  return (
    <div className="view-stack">
      {blockedByBaseline && (
        <Callout tone="warn" title="Plan not executed">
          <p>The original repository did not produce a usable baseline, so no step was applied. ReForge never modifies code it cannot verify.</p>
        </Callout>
      )}
      <Panel title={`Plan v${plan.version}`} actions={<span className="muted">{plan.steps.length} step{plan.steps.length === 1 ? "" : "s"}</span>}>
        <p className="prose">{plan.rationale}</p>
        <ol className="step-list">
          {plan.steps.map((step) => {
            const error = stepError(state, step.id);
            return (
              <li key={step.id} className="step-item">
                <div className="step-head">
                  <span className="step-id">{step.id}</span>
                  <span className="step-title">{step.title}</span>
                  <Tag tone="neutral">{label(step.kind)}</Tag>
                  <Tag tone={STEP_TONE[step.status]}>{titleCase(step.status)}</Tag>
                </div>
                <p className="step-description">{step.description}</p>
                <dl className="step-meta">
                  <div><dt>Files</dt><dd className="mono">{step.files.join(", ") || "—"}</dd></div>
                  {step.depends_on.length > 0 && <div><dt>Depends on</dt><dd>{step.depends_on.join(", ")}</dd></div>}
                  <div><dt>Validation</dt><dd>{step.validation_hint}</dd></div>
                  {step.commit_sha && <div><dt>Commit</dt><dd className="mono">{step.commit_sha.slice(0, 12)}</dd></div>}
                </dl>
                {error && <p className="step-error">Not applied: {error}</p>}
              </li>
            );
          })}
        </ol>
      </Panel>
    </div>
  );
}

function stepError(state: ProjectState, stepId: string): string | null {
  const event = [...state.trace].reverse().find((item) => item.agent === "transformation" && item.summary.startsWith(`${stepId} `));
  return typeof event?.data.error === "string" ? event.data.error : null;
}
