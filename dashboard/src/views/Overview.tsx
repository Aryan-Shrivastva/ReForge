import type { ProjectState } from "../types";
import { compactNumber, duration, failureReason, label, runDuration, runStatus, titleCase } from "../lib/format";
import { Callout, KeyValues, Panel, Stat, StatGrid, Tag } from "../components/ui";

const SESSION_KEYS: Array<[string, string]> = [
  ["stage", "Stage"],
  ["languages", "Languages"],
  ["plan_summary", "Plan"],
  ["repair_history", "Repair history"],
  ["agent_calls", "ADK agent calls"],
  ["agent_history", "Agent history"],
];

export function Overview({ state, onOpenTests }: { state: ProjectState; onOpenTests: () => void }) {
  const status = runStatus(state);
  const reason = failureReason(state);
  const latest = state.test_runs.at(-1);
  const tokens = state.metrics.tokens_in + state.metrics.tokens_out;
  const maxTokens = state.budgets.max_tokens ?? 0;
  const lastEvent = state.trace.at(-1);

  return (
    <div className="view-stack">
      {reason && (
        <Callout tone={status === "PARTIAL" ? "warn" : "bad"} title={status === "PARTIAL" ? "Run finished partially" : status === "ABORTED" ? "Run was cancelled" : "Run failed"}>
          <p>{reason}</p>
          {state.test_runs.some((report) => !report.install_ok) && (
            <button type="button" className="link-button" onClick={onOpenTests}>View install log</button>
          )}
        </Callout>
      )}
      {!state.final_status && lastEvent && (
        <Callout tone="info" title="In progress">
          <p>{lastEvent.summary}</p>
        </Callout>
      )}

      <StatGrid>
        <Stat label="Baseline tests" value={state.baseline_pass_set.length} hint={state.baseline?.generated_tests ? "includes generated tests" : undefined} />
        <Stat label="Passing now" value={latest ? latest.passed.length : "—"} hint={latest ? `${latest.failed.length + latest.errored.length} failing` : undefined} />
        <Stat label="Files changed" value={state.metrics.files_changed} hint={`+${state.metrics.lines_added} / −${state.metrics.lines_removed}`} />
        <Stat label="Repairs" value={state.repairs.length} hint={`${state.replans} replan${state.replans === 1 ? "" : "s"}`} />
        <Stat label="Duration" value={duration(runDuration(state))} />
        <Stat label="Tokens" value={compactNumber(tokens)} hint={maxTokens ? `of ${compactNumber(maxTokens)} budget` : undefined} />
      </StatGrid>

      <div className="two-column">
        <Panel title="Run details">
          <KeyValues
            items={[
              ["Objective", state.objective],
              ["Origin", titleCase(state.objective_origin ?? "user")],
              ["Repository", <span className="mono">{state.repo_source}</span>],
              ["Work branch", <span className="mono">{state.work_branch}</span>],
              ["Base commit", <span className="mono">{state.base_commit ? state.base_commit.slice(0, 12) : "—"}</span>],
              ["Languages", state.analysis?.languages?.length ? state.analysis.languages.map(label).join(", ") : "—"],
            ]}
          />
        </Panel>
        <AgentsPanel state={state} />
      </div>
    </div>
  );
}

/** The adk-web compose service serves the ADK dev UI on port 8001 of the same host. */
function adkWebUrl(runId: string): string {
  const base = import.meta.env.VITE_ADK_WEB_URL ?? `${window.location.protocol}//${window.location.hostname}:8001`;
  return `${base}/dev-ui/?app=reforge&session=${encodeURIComponent(runId)}`;
}

function AgentsPanel({ state }: { state: ProjectState }) {
  const runtime = Object.entries(state.agent_runtime ?? {});
  const session = state.agent_session ?? {};
  const sessionRows = SESSION_KEYS.filter(([key]) => session[key] !== undefined && session[key] !== "");
  const usesAdk = runtime.some(([, framework]) => framework === "adk");
  return (
    <Panel
      title="Agents"
      actions={usesAdk ? <a className="button" href={adkWebUrl(state.run_id)} target="_blank" rel="noreferrer">Open in ADK web UI</a> : undefined}
    >
      {runtime.length ? (
        <table className="agents-table">
          <thead>
            <tr><th scope="col">Role</th><th scope="col">Runtime</th><th scope="col">Calls</th></tr>
          </thead>
          <tbody>
            {runtime.map(([role, framework]) => (
              <tr key={role}>
                <td>{label(role)}</td>
                <td><Tag tone={framework === "adk" ? "info" : "neutral"}>{framework === "adk" ? "Google ADK" : "CrewAI"}</Tag></td>
                <td className="numeric">{state.metrics.agent_calls[role] ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="empty-inline">Runtime assignment is recorded when the run starts.</p>
      )}
      {sessionRows.length > 0 && (
        <details className="disclosure">
          <summary>ADK session state</summary>
          <KeyValues items={sessionRows.map(([key, label]) => [label, <span className="pre-line">{String(session[key])}</span>])} />
        </details>
      )}
    </Panel>
  );
}
