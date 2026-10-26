import { useState, type ReactNode } from "react";
import { api } from "../api";
import type { ProjectState, TestReport } from "../types";
import { compactNumber, duration, relativeTime, repoName, runDuration, runStatus, startedAt, titleCase } from "../lib/format";
import { buildSteps, explain, languageName, type Step, type StepKey } from "../lib/steps";
import { ApprovalPanel, type ReviewDecision } from "../components/ApprovalPanel";
import { Chip, Meta, Status, StepIcon } from "../components/ui";
import { Changes } from "./Changes";
import { Logs } from "./Logs";
import { Report } from "./Report";

export const RUN_TABS = [
  ["overview", "Overview"],
  ["changes", "Changes"],
  ["logs", "Logs"],
  ["report", "Report"],
] as const;
export type RunTab = (typeof RUN_TABS)[number][0];

export function RunPage({ state, tab, onTab, onCancel, onDelete, onRerun, onReview }: {
  state: ProjectState;
  tab: RunTab;
  onTab: (tab: RunTab) => void;
  onCancel: () => void;
  onDelete: () => void;
  onRerun: () => void;
  onReview: (review: ReviewDecision) => Promise<boolean>;
}) {
  const status = runStatus(state);
  return (
    <div className="container">
      <header className="run-head">
        <div className="run-head-main">
          <h1>{repoName(state)}</h1>
          <p className="muted run-objective">{state.objective}</p>
        </div>
        <div className="run-head-actions">
          {!state.final_status && <button type="button" className="btn btn-danger" onClick={onCancel}>Cancel</button>}
          <button type="button" className="btn" onClick={onRerun}>Rerun</button>
          {state.final_status && <button type="button" className="btn btn-danger" onClick={onDelete}>Delete</button>}
          {state.report_path && <a className="btn" href={api.reportUrl(state.run_id, "markdown")} download={`reforge-${state.run_id.slice(0, 8)}.md`}>Download report</a>}
        </div>
      </header>

      <Meta
        items={[
          ["Status", <Status status={status} />],
          ["Duration", duration(runDuration(state))],
          ["Started", relativeTime(startedAt(state))],
          ["Branch", <span className="mono">{state.work_branch}</span>],
          ["Language", state.analysis?.languages?.length ? state.analysis.languages.map(languageName).join(", ") : "—"],
        ]}
      />

      <nav className="tabs" aria-label="Run sections">
        {RUN_TABS.map(([key, label]) => (
          <button key={key} type="button" className={tab === key ? "active" : ""} aria-current={tab === key ? "page" : undefined} onClick={() => onTab(key)}>{label}</button>
        ))}
      </nav>

      {tab === "overview" && <Overview state={state} onTab={onTab} onReview={onReview} />}
      {tab === "changes" && <Changes state={state} />}
      {tab === "logs" && <Logs state={state} />}
      {tab === "report" && <Report state={state} />}
    </div>
  );
}

function Overview({ state, onTab, onReview }: { state: ProjectState; onTab: (tab: RunTab) => void; onReview: (review: ReviewDecision) => Promise<boolean> }) {
  const status = runStatus(state);
  const explanation = explain(state);
  const steps = buildSteps(state);
  const firstProblem = steps.find((step) => step.status === "failed" || step.status === "warning")?.key;
  const [open, setOpen] = useState<Set<StepKey>>(() => new Set(firstProblem ? [firstProblem] : []));
  const toggle = (key: StepKey) => setOpen((current) => {
    const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <div className="overview">
      <section className={`summary summary-${status.toLowerCase()}`}>
        <h2>{explanation.headline}</h2>
        <p>{explanation.detail}</p>
        {explanation.nextSteps.length > 0 && (
          <ul className="next-steps">{explanation.nextSteps.map((item) => <li key={item}>{item}</li>)}</ul>
        )}
      </section>

      {status === "AWAITING" && <ApprovalPanel key={`${state.run_id}-${state.plan?.version}`} state={state} onReview={onReview} />}

      <section aria-label="Steps">
        <h2 className="steps-title">Steps</h2>
        <ol className="steps">
          {steps.map((step) => (
            <li key={step.key} className={`step step-${step.status}`}>
              <button
                type="button"
                className="step-row"
                onClick={() => toggle(step.key)}
                aria-expanded={open.has(step.key)}
                disabled={step.status === "pending" || step.status === "skipped"}
              >
                <StepIcon status={step.status} />
                <span className="step-label">{step.label}</span>
                <span className="step-summary">{step.summary}</span>
                <span className="step-time">{step.seconds !== null ? duration(step.seconds) : ""}</span>
                <svg className="step-chevron" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>
              </button>
              {open.has(step.key) && <div className="step-body"><StepDetail state={state} step={step} onTab={onTab} /></div>}
            </li>
          ))}
        </ol>
      </section>

      <AgentsAndUsage state={state} />
    </div>
  );
}

function StepDetail({ state, step, onTab }: { state: ProjectState; step: Step; onTab: (tab: RunTab) => void }) {
  const analysis = state.analysis;
  const plan = state.plan;
  switch (step.key) {
    case "ANALYZE":
      if (!analysis) return <p className="muted">The analysis did not complete. See Logs.</p>;
      return (
        <>
          <p>{analysis.summary}</p>
          {analysis.dependencies.length > 0 && (
            <Detail title={`Dependencies (${analysis.dependencies.length})`}>
              <table className="table">
                <thead><tr><th>Package</th><th>Version</th><th>Status</th><th>Notes</th></tr></thead>
                <tbody>
                  {analysis.dependencies.map((item) => (
                    <tr key={item.name}>
                      <td className="mono">{item.name}</td>
                      <td className="mono">{item.current ?? "—"}{item.target ? ` → ${item.target}` : ""}</td>
                      <td><Chip tone={item.status === "ok" ? "good" : item.status === "deprecated" || item.status === "incompatible" ? "bad" : "warn"}>{titleCase(item.status)}</Chip></td>
                      <td>{item.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Detail>
          )}
          {analysis.deprecated_patterns.length > 0 && (
            <Detail title={`Deprecated code (${analysis.deprecated_patterns.length})`}>
              <ul className="findings">
                {analysis.deprecated_patterns.map((item) => (
                  <li key={`${item.rule_id}-${item.file}-${item.line}`}>
                    <span className="mono">{item.file}:{item.line}</span>
                    <code>{item.snippet}</code>
                    <span className="muted">{item.replacement_hint}</span>
                  </li>
                ))}
              </ul>
            </Detail>
          )}
          {analysis.risks.length > 0 && (
            <Detail title="Risks">
              <ul className="bullets">{analysis.risks.map((risk) => <li key={risk}>{risk}</li>)}</ul>
            </Detail>
          )}
        </>
      );
    case "PLAN":
      if (!plan) return <p className="muted">No plan was produced.</p>;
      return (
        <>
          <p>{plan.rationale}</p>
          <ol className="plan">
            {plan.steps.map((item) => (
              <li key={item.id}>
                <span className="plan-id mono">{item.id}</span>
                <div>
                  <strong>{item.title}</strong>
                  <p className="muted">{item.description}</p>
                  <p className="plan-files mono">{item.files.join(", ")}</p>
                </div>
              </li>
            ))}
          </ol>
        </>
      );
    case "BASELINE":
      return <BaselineDetail state={state} />;
    case "TRANSFORM":
      if (!plan) return <p className="muted">Nothing to apply.</p>;
      return (
        <>
          <ul className="changes">
            {plan.steps.map((item) => {
              const error = stepError(state, item.id);
              return (
                <li key={item.id}>
                  <Chip tone={item.status === "applied" ? "good" : item.status === "failed" ? "bad" : item.status === "skipped" ? "warn" : "neutral"}>{titleCase(item.status)}</Chip>
                  <div>
                    <strong>{item.title}</strong>
                    {item.commit_sha && <span className="muted mono"> · {item.commit_sha.slice(0, 7)}</span>}
                    {error && <p className="error-text">{error}</p>}
                  </div>
                </li>
              );
            })}
          </ul>
          {state.metrics.files_changed > 0 && <button type="button" className="btn btn-sm" onClick={() => onTab("changes")}>View changes</button>}
        </>
      );
    case "TEST":
      return <VerifyDetail state={state} />;
    case "REPORT":
      return (
        <div className="inline-actions">
          <a className="btn btn-sm" href={api.reportUrl(state.run_id, "markdown")} download>Markdown</a>
          <a className="btn btn-sm" href={api.reportUrl(state.run_id, "json")} download>JSON</a>
          <button type="button" className="btn btn-sm" onClick={() => onTab("report")}>Read report</button>
        </div>
      );
  }
}

function BaselineDetail({ state }: { state: ProjectState }) {
  const report = state.baseline ?? state.test_runs.find((item) => item.phase === "baseline");
  if (!report) return <p className="muted">Not run.</p>;
  if (!report.install_ok) {
    return (
      <>
        <p>Dependencies could not be installed in the sandbox, so nothing was tested or changed. Install output:</p>
        <pre className="log">{report.install_log_tail || "No output was captured."}</pre>
      </>
    );
  }
  return (
    <>
      {state.smoke_baseline ? (
        <p>This repository has no runnable tests and no generated test passed, so ReForge recorded a <strong>syntax check</strong> for each source file. Changes are verified to install and parse; behavior is not tested.</p>
      ) : report.generated_tests ? (
        <p>The repository had too few tests, so ReForge generated characterization tests that describe its current behavior. They passed on the original code and now guard the changes.</p>
      ) : (
        <p>The repository's own tests ran on the original code. Every passing test is frozen and must still pass after the changes.</p>
      )}
      <TestList title="Recorded" ids={state.baseline_pass_set} />
      <Failures report={report} />
    </>
  );
}

function VerifyDetail({ state }: { state: ProjectState }) {
  const latest = state.test_runs.at(-1);
  if (!latest || latest === state.baseline) return <p className="muted">Not run.</p>;
  const missing = state.baseline_pass_set.filter((id) => !latest.passed.includes(id));
  return (
    <>
      {missing.length ? (
        <p className="error-text">{missing.length} recorded {state.smoke_baseline ? "check" : "test"}{missing.length === 1 ? " no longer passes" : "s no longer pass"}.</p>
      ) : (
        <p>Every recorded {state.smoke_baseline ? "check" : "test"} still passes.</p>
      )}
      {missing.length > 0 && <TestList title="Regressed" ids={missing} open />}
      {state.repairs.length > 0 && (
        <Detail title={`Automatic repairs (${state.repairs.length})`} open>
          <ol className="repairs">
            {state.repairs.map((repair) => (
              <li key={repair.iteration}>
                <span className="mono">R{repair.iteration}</span>
                <span>{repair.diagnosis}</span>
                <span className="muted">{repair.failing_before} → {repair.failing_after ?? "…"} failing</span>
              </li>
            ))}
          </ol>
        </Detail>
      )}
      <Failures report={latest} />
    </>
  );
}

function Failures({ report }: { report: TestReport }) {
  const failures = [...report.failed, ...report.errored];
  if (!failures.length) return null;
  return (
    <Detail title={`Failures (${failures.length})`}>
      {failures.map((failure) => (
        <div key={failure.test_id} className="failure">
          <p className="mono">{failure.test_id}</p>
          <p className="error-text">{failure.message}</p>
          {failure.traceback_excerpt && <pre className="log">{failure.traceback_excerpt}</pre>}
        </div>
      ))}
    </Detail>
  );
}

function TestList({ title, ids, open = false }: { title: string; ids: string[]; open?: boolean }) {
  if (!ids.length) return null;
  return (
    <Detail title={`${title} (${ids.length})`} open={open}>
      <ul className="test-ids">{ids.map((id) => <li key={id} className="mono">{id.replace(/^smoke:syntax:/, "syntax · ")}</li>)}</ul>
    </Detail>
  );
}

function Detail({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details className="detail" open={open}>
      <summary>{title}</summary>
      <div className="detail-body">{children}</div>
    </details>
  );
}

function AgentsAndUsage({ state }: { state: ProjectState }) {
  const runtime = Object.entries(state.agent_runtime ?? {});
  const tokens = state.metrics.tokens_in + state.metrics.tokens_out;
  const usesAdk = runtime.some(([, framework]) => framework === "adk");
  return (
    <section className="agents" aria-label="Agents">
      <div className="agents-head">
        <h2 className="steps-title">Agents</h2>
        {usesAdk && <a className="link" href={adkWebUrl(state.run_id)} target="_blank" rel="noreferrer">Open session in ADK web UI ↗</a>}
      </div>
      <div className="agents-grid">
        {runtime.map(([role, framework]) => (
          <div key={role} className="agent">
            <span>{titleCase(role)}</span>
            <span className="muted">{framework === "adk" ? "Google ADK" : "CrewAI"} · {state.metrics.agent_calls[role] ?? 0} call{(state.metrics.agent_calls[role] ?? 0) === 1 ? "" : "s"}</span>
          </div>
        ))}
        <div className="agent">
          <span>Tokens</span>
          <span className="muted">{compactNumber(tokens)} of {compactNumber(state.budgets.max_tokens ?? 0)} budget</span>
        </div>
      </div>
    </section>
  );
}

/** The adk-web compose service serves the ADK dev UI on port 8001 of the same host. */
function adkWebUrl(runId: string): string {
  const base = import.meta.env.VITE_ADK_WEB_URL ?? `${window.location.protocol}//${window.location.hostname}:8001`;
  return `${base}/dev-ui/?app=reforge&session=${encodeURIComponent(runId)}`;
}

function stepError(state: ProjectState, stepId: string): string | null {
  const event = [...state.trace].reverse().find((item) => item.agent === "transformation" && item.summary.startsWith(`${stepId} `));
  return typeof event?.data.error === "string" ? event.data.error : null;
}
