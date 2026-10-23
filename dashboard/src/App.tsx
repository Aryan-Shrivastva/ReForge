import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { Benchmark, EvaluationSummary, ProjectState, TestReport, TraceEvent } from "./types";

const stages = ["INIT", "ANALYZE", "PLAN", "BASELINE", "TRANSFORM", "TEST", "REPAIR", "REPLAN", "REPORT", "DONE"];
type View = "overview" | "analysis" | "plan" | "diff" | "tests" | "trace" | "report" | "benchmarks";

function App() {
  const [runs, setRuns] = useState<ProjectState[]>([]);
  const [selected, setSelected] = useState<ProjectState | null>(null);
  const [view, setView] = useState<View>("overview");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshRuns = async () => {
    try {
      const next = await api.listRuns();
      setRuns(next);
      // Historical runs remain available in the sidebar, but a fresh dashboard
      // should guide the user to provide a repository rather than auto-opening
      // an old failure. A newly created run is selected explicitly by startRun.
      setSelected((current) => current ? next.find((run) => run.run_id === current.run_id) ?? current : null);
      setError(null);
      return true;
    } catch (caught) {
      setError(message(caught));
      return false;
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    let active = true;
    let retryTimer: number | undefined;
    let attempts = 0;

    const load = async () => {
      const connected = await refreshRuns();
      if (!connected && active && attempts < 3) {
        attempts += 1;
        retryTimer = window.setTimeout(() => { void load(); }, 1_000);
      }
    };

    void load();
    return () => {
      active = false;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, []);

  useEffect(() => {
    if (!selected || selected.final_status) return;
    const events = new EventSource(api.eventUrl(selected.run_id));
    events.addEventListener("trace", () => { void refreshSelected(selected.run_id); });
    events.addEventListener("complete", () => { void refreshRuns(); });
    events.onerror = () => events.close();
    return () => events.close();
  }, [selected?.run_id, selected?.final_status]);

  const refreshSelected = async (runId: string) => {
    try {
      const current = await api.getRun(runId);
      setSelected(current);
      setRuns((items) => [current, ...items.filter((item) => item.run_id !== current.run_id)]);
    } catch (caught) {
      setError(message(caught));
    }
  };

  const startRun = async (input: { objective?: string; repoUrl?: string; upload?: File }) => {
    try {
      const created = await api.createRun(input);
      await refreshSelected(created.run_id);
      setView("overview");
    } catch (caught) {
      setError(message(caught));
    }
  };

  const cancelRun = async () => {
    if (!selected) return;
    try {
      const cancelled = await api.cancelRun(selected.run_id);
      setSelected(cancelled);
      void refreshRuns();
    } catch (caught) {
      setError(message(caught));
    }
  };

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand"><span>RF</span><div>ReForge<small>Modernization control plane</small></div></div>
        <RunForm onStart={startRun} />
        <div className="run-list-heading">Runs</div>
        {loading && <p className="muted">Loading persisted runs…</p>}
        {runs.map((run) => (
          <button className={`run-row ${selected?.run_id === run.run_id ? "selected" : ""}`} key={run.run_id}
            onClick={() => { setSelected(run); setView("overview"); }}>
            <span className={`status-dot ${effectiveFinalStatus(run)?.toLowerCase() ?? "active"}`} />
            <span><strong>{run.objective}</strong><small>{run.run_id.slice(0, 8)} · {run.stage}</small></span>
          </button>
        ))}
      </aside>
      <section className="content">
        <header className="topbar">
          <div>{selected ? <><p className="eyebrow">{selected.work_branch}</p><h1>{selected.objective}</h1></> : <><p className="eyebrow">ReForge dashboard</p><h1>Choose or start a modernization run</h1></>}</div>
          {selected && <div className="actions"><StatusBadge status={effectiveFinalStatus(selected) ?? selected.stage} />{!selected.final_status && <button className="danger" onClick={() => void cancelRun()}>Cancel run</button>}</div>}
        </header>
        {error && <div className="error"><span>{error}</span><button onClick={() => setError(null)}>Dismiss</button></div>}
        <nav className="tabs">
          {(["overview", "analysis", "plan", "diff", "tests", "trace", "report", "benchmarks"] as View[]).map((item) =>
            <button key={item} className={view === item ? "active" : ""} onClick={() => setView(item)}>{item}</button>
          )}
        </nav>
        {view === "benchmarks" ? <Benchmarks onError={setError} /> : selected ? <RunView view={view} state={selected} onError={setError} /> : <EmptyState />}
      </section>
    </main>
  );
}

function RunForm({ onStart }: { onStart: (input: { objective?: string; repoUrl?: string; upload?: File }) => Promise<void> }) {
  const [objective, setObjective] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [upload, setUpload] = useState<File>();
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!repoUrl && !upload) return;
    setBusy(true);
    await onStart({ objective, repoUrl: upload ? undefined : repoUrl, upload });
    setBusy(false);
  };
  return <form className="run-form" onSubmit={submit}>
    <label>Optional focus<textarea value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="Leave blank for agent-led repository discovery, or describe a goal such as: upgrade Flask 1.x to 3.x" /></label>
    <label>Git URL<input value={repoUrl} disabled={Boolean(upload)} onChange={(event) => setRepoUrl(event.target.value)} placeholder="https://…" /></label>
    <label className="upload">or ZIP<input type="file" accept=".zip" onChange={(event) => { setUpload(event.target.files?.[0]); if (event.target.files?.[0]) setRepoUrl(""); }} /></label>
    <button type="submit" disabled={busy}>{busy ? "Queueing…" : "Start run"}</button>
  </form>;
}

function RunView({ view, state, onError }: { view: Exclude<View, "benchmarks">; state: ProjectState; onError: (value: string) => void }) {
  if (view === "overview") return <Overview state={state} />;
  if (view === "analysis") return <Analysis state={state} />;
  if (view === "plan") return <Plan state={state} />;
  if (view === "diff") return <Diff state={state} onError={onError} />;
  if (view === "tests") return <Tests state={state} />;
  if (view === "trace") return <Trace state={state} />;
  return <Report state={state} onError={onError} />;
}

function Overview({ state }: { state: ProjectState }) {
  return <div className="stack"><Pipeline state={state} /><div className="metric-grid"><Metric label="Baseline tests" value={String(state.baseline_pass_set.length)} /><Metric label="Repairs" value={String(state.repairs.length)} /><Metric label="Replans" value={String(state.replans)} /><Metric label="Wall clock" value={`${state.metrics.wall_clock_s.toFixed(1)}s`} /></div><section className="card"><h2>Live decision</h2><p>{state.trace.at(-1)?.summary ?? "The run is queued."}</p></section></div>;
}

function Pipeline({ state }: { state: ProjectState }) {
  const active = stages.indexOf(state.stage);
  return <section className="card"><div className="section-heading"><div><p className="eyebrow">Deterministic orchestration</p><h2>Pipeline</h2></div><span className="muted">{state.stage}</span></div><div className="pipeline">{stages.map((stage, index) => <div key={stage} className={`pipeline-node ${index < active || state.final_status ? "done" : ""} ${index === active && !state.final_status ? "current" : ""}`}><span>{index + 1}</span><strong>{stage}</strong></div>)}</div><div className="repair-loop">TEST <span>⇄</span> REPAIR <span>⇢</span> REPLAN when progress stalls</div></section>;
}

function Analysis({ state }: { state: ProjectState }) {
  const analysis = state.analysis;
  if (!analysis) return <Pending label="Analysis" />;
  return <div className="stack"><section className="card"><h2>Analysis summary</h2><p>{analysis.summary}</p>{analysis.risks.length > 0 && <p className="risk">Risks: {analysis.risks.join(" · ")}</p>}</section><Table headers={["Dependency", "Current", "Target", "Status", "Reason"]} rows={analysis.dependencies.map((item) => [item.name, item.current ?? "—", item.target ?? "—", item.status, item.reason])} /><Table headers={["Rule", "Location", "Snippet", "Replacement"]} rows={analysis.deprecated_patterns.map((item) => [item.rule_id, `${item.file}:${item.line}`, item.snippet, item.replacement_hint])} empty="No targeted deprecated patterns found." /></div>;
}

function Plan({ state }: { state: ProjectState }) {
  if (!state.plan) return <Pending label="Plan" />;
  const baselineAttempt = state.test_runs.find((report) => report.phase === "baseline");
  const baselineBlocked = !state.baseline && Boolean(baselineAttempt?.failed.length || baselineAttempt?.errored.length || baselineAttempt && !baselineAttempt.install_ok);
  return <section className="card"><div className="section-heading"><div><p className="eyebrow">Version {state.plan.version}</p><h2>Modernization plan</h2></div></div><p>{state.plan.rationale}</p>{baselineBlocked && <p className="risk">Plan steps are pending because the original repository did not pass its baseline build/test check. ReForge made no source changes, so it does not mistake a pre-existing failure for a successful modernization.</p>}<div className="steps">{state.plan.steps.map((step) => <article className={`step ${step.status}`} key={step.id}><span className="step-id">{step.id}</span><div><h3>{step.title}</h3><p>{step.description}</p><small>{step.files.join(", ") || "No files"} · validates: {step.validation_hint}</small>{planStepError(state, step.id) && <p className="risk">Not applied: {planStepError(state, step.id)}</p>}</div><StatusBadge status={step.status} /></article>)}</div></section>;
}

function Diff({ state, onError }: { state: ProjectState; onError: (value: string) => void }) {
  const [step, setStep] = useState("");
  const [diff, setDiff] = useState("Select a plan step or load the complete diff.");
  const chosen = state.plan?.steps.find((item) => item.id === step);
  const load = async () => {
    if (chosen && chosen.status !== "applied") {
      setDiff(`${chosen.id} has no diff because it was ${chosen.status}. ${planStepError(state, chosen.id) ?? "No files were changed."}`);
      return;
    }
    try {
      const loaded = await api.getDiff(state.run_id, step || undefined);
      setDiff(loaded || "No code change was produced for this run.");
    } catch (caught) {
      const detail = message(caught);
      setDiff(`Diff is unavailable: ${detail}`);
      onError(detail);
    }
  };
  return <section className="card"><div className="section-heading"><h2>Diff viewer</h2><div className="inline"><select value={step} onChange={(event) => setStep(event.target.value)}><option value="">Whole run</option>{state.plan?.steps.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.title} ({item.status})</option>)}</select><button onClick={() => void load()}>Load diff</button></div></div><pre className="diff">{diff}</pre></section>;
}

function Tests({ state }: { state: ProjectState }) {
  const latest = state.test_runs.at(-1);
  const baselineAttempt = state.test_runs.find((report) => report.phase === "baseline");
  return <div className="stack"><section className="card"><h2>Baseline versus latest</h2><div className="metric-grid"><Metric label="Frozen baseline" value={String(state.baseline_pass_set.length)} /><Metric label="Latest passed" value={String(latest?.passed.length ?? 0)} /><Metric label="Latest failures" value={String((latest?.failed.length ?? 0) + (latest?.errored.length ?? 0))} /><Metric label="Generated tests" value={state.baseline?.generated_tests ? "yes" : "no"} /></div></section><TestReportCard title={state.baseline ? "Baseline" : "Baseline attempt"} report={state.baseline ?? baselineAttempt} />{state.baseline && latest !== state.baseline && <TestReportCard title="Latest verification" report={latest} />}<section className="card"><h2>Repair history</h2>{state.repairs.length ? state.repairs.map((repair) => <p key={repair.iteration}>R{repair.iteration}: {repair.diagnosis} · {repair.failing_before} → {repair.failing_after ?? "pending"}</p>) : <p className="muted">No repair attempts.</p>}</section></div>;
}

function TestReportCard({ title, report }: { title: string; report?: TestReport | null }) {
  if (!report) return <Pending label={title} />;
  const failures = [...report.failed, ...report.errored];
  return <section className="card"><div className="section-heading"><h2>{title}</h2><StatusBadge status={report.install_ok && failures.length === 0 ? "passed" : "failed"} /></div><p>{report.passed.length} passed · {failures.length} failed/error · {report.skipped.length} skipped · {report.duration_s.toFixed(2)}s</p>{report.generated_tests && <p className="muted">This result includes ReForge-generated characterization tests; these are separate from the repository’s own test suite.</p>}{report.passed.length > 0 && <details><summary>Passed tests ({report.passed.length})</summary><ul>{report.passed.map((test) => <li key={test}><code>{test}</code></li>)}</ul></details>}{report.skipped.length > 0 && <details><summary>Skipped tests ({report.skipped.length})</summary><ul>{report.skipped.map((test) => <li key={test}><code>{test}</code></li>)}</ul></details>}{failures.map((failure) => <details key={failure.test_id}><summary>{failure.test_id}: {failure.message}</summary><pre>{failure.traceback_excerpt}</pre></details>)}</section>;
}

function Trace({ state }: { state: ProjectState }) {
  const [agent, setAgent] = useState("all");
  const agents = [...new Set(state.trace.map((event) => event.agent))];
  const events = state.trace.filter((event) => agent === "all" || event.agent === agent);
  return <section className="card"><div className="section-heading"><h2>Agent trace</h2><select value={agent} onChange={(event) => setAgent(event.target.value)}><option value="all">All agents</option>{agents.map((name) => <option key={name}>{name}</option>)}</select></div><div className="trace">{events.map((event, index) => <TraceRow event={event} key={`${event.ts}-${index}`} />)}</div></section>;
}

function TraceRow({ event }: { event: TraceEvent }) {
  return <article className={`trace-row ${event.kind}`}><time>{new Date(event.ts).toLocaleTimeString()}</time><StatusBadge status={event.kind} /><div><strong>{event.agent} · {event.stage}</strong><p>{event.summary}</p></div></article>;
}

function Report({ state, onError }: { state: ProjectState; onError: (value: string) => void }) {
  const [report, setReport] = useState(state.final_status ? "Load the generated report." : "The final report will be available when this run completes.");
  const load = async () => { try { setReport(await api.getReport(state.run_id)); } catch (caught) { onError(message(caught)); } };
  return <section className="card"><div className="section-heading"><h2>Final report</h2>{state.final_status && <div className="inline"><button onClick={() => void load()}>Load report</button>{state.report_path && <><a className="download" href={api.reportUrl(state.run_id, "markdown")} download="reforge-report.md">Markdown</a><a className="download" href={api.reportUrl(state.run_id, "json")} download="reforge-report.json">JSON</a></>}</div>}</div>{isLegacyFalseSuccess(state) && <p className="risk">This older run was saved as SUCCESS by the previous workflow. Because plan steps failed, its effective outcome is PARTIAL; its stored report is retained for audit.</p>}<pre className="report">{report}</pre></section>;
}

function Benchmarks({ onError }: { onError: (value: string) => void }) {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [summary, setSummary] = useState<EvaluationSummary | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.listBenchmarks().then(setBenchmarks).catch((caught) => onError(message(caught))); }, []);
  const run = async () => { setBusy(true); try { setSummary(await api.runBenchmarks({ repetitions: 3, ablation: "iterative" })); } catch (caught) { onError(message(caught)); } finally { setBusy(false); } };
  return <div className="stack"><section className="card"><div className="section-heading"><div><p className="eyebrow">M8 controlled suite</p><h2>Benchmarks</h2></div><button onClick={() => void run()} disabled={busy}>{busy ? "Running 3× suite…" : "Run 3× suite"}</button></div><Table headers={["Benchmark", "Original tests", "Objective"]} rows={benchmarks.map((item) => [item.name, item.has_tests ? "yes" : "no", item.objective])} /></section>{summary && <section className="card"><h2>Aggregate metrics</h2><div className="metric-grid"><Metric label="Success rate" value={percent(summary.success_rate)} /><Metric label="Partial rate" value={percent(summary.partial_rate)} /><Metric label="Runs" value={String(summary.runs.length)} /></div>{summary.artifact_path && <p className="muted">Saved to {summary.artifact_path}</p>}<Table headers={["Benchmark", "Status", "Behavior", "Repairs", "Replans", "Regressions", "Time"]} rows={summary.runs.map((item) => [item.benchmark, item.status ?? "—", percent(item.behavior_preservation), String(item.repair_iterations), String(item.replans), String(item.regressions_introduced), `${item.wall_clock_s.toFixed(1)}s`])} /></section>}</div>;
}

function Table({ headers, rows, empty }: { headers: string[]; rows: string[][]; empty?: string }) {
  return <section className="card table-card"><table><thead><tr>{headers.map((header) => <th key={header}>{header}</th>)}</tr></thead><tbody>{rows.length ? rows.map((row, index) => <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>) : <tr><td colSpan={headers.length} className="muted">{empty ?? "No data yet."}</td></tr>}</tbody></table></section>;
}

function Metric({ label, value }: { label: string; value: string }) { return <article className="metric"><span>{label}</span><strong>{value}</strong></article>; }
function planStepError(state: ProjectState, stepId: string) { const event = [...state.trace].reverse().find((item) => item.agent === "transformation" && item.summary.startsWith(`${stepId} `)); return typeof event?.data.error === "string" ? event.data.error : null; }
function isLegacyFalseSuccess(state: ProjectState) { return state.final_status === "SUCCESS" && Boolean(state.plan?.steps.some((step) => step.status !== "applied")); }
function effectiveFinalStatus(state: ProjectState) { return isLegacyFalseSuccess(state) ? "PARTIAL" : state.final_status; }
function Pending({ label }: { label: string }) { return <section className="card pending"><h2>{label}</h2><p>Waiting for the deterministic workflow to reach this checkpoint.</p></section>; }
function EmptyState() { return <section className="empty"><h2>Start a controlled modernization run</h2><p>Provide a Git repository URL or a ZIP archive, then follow every state transition, test result, diff, and repair decision here.</p></section>; }
function StatusBadge({ status }: { status: string }) { return <span className={`badge ${status.toLowerCase().replaceAll("_", "-")}`}>{status}</span>; }
function percent(value: number) { return `${Math.round(value * 100)}%`; }
function message(value: unknown) { return value instanceof Error ? value.message : String(value); }

export default App;
