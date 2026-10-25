import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { ProjectState } from "./types";
import { duration, label, message, relativeTime, repoName, runDuration, runStatus, startedAt } from "./lib/format";
import { NewRunDialog, type NewRunInput } from "./components/NewRunDialog";
import { RunList } from "./components/RunList";
import { StageTimeline } from "./components/StageTimeline";
import { StatusPill } from "./components/ui";
import { Activity } from "./views/Activity";
import { Analysis } from "./views/Analysis";
import { Benchmarks } from "./views/Benchmarks";
import { Changes } from "./views/Changes";
import { Overview } from "./views/Overview";
import { Plan } from "./views/Plan";
import { Report } from "./views/Report";
import { Tests } from "./views/Tests";

const TABS = [
  ["overview", "Overview"],
  ["analysis", "Analysis"],
  ["plan", "Plan"],
  ["changes", "Changes"],
  ["tests", "Tests"],
  ["activity", "Activity"],
  ["report", "Report"],
] as const;
type Tab = (typeof TABS)[number][0];

interface Route {
  page: "runs" | "benchmarks";
  runId: string | null;
  tab: Tab;
}

function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "benchmarks") return { page: "benchmarks", runId: null, tab: "overview" };
  const tab = TABS.some(([key]) => key === parts[2]) ? (parts[2] as Tab) : "overview";
  return { page: "runs", runId: parts[0] === "runs" && parts[1] ? parts[1] : null, tab };
}

function routeHash(route: Route): string {
  if (route.page === "benchmarks") return "#/benchmarks";
  if (!route.runId) return "#/runs";
  return `#/runs/${route.runId}${route.tab === "overview" ? "" : `/${route.tab}`}`;
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [runs, setRuns] = useState<ProjectState[]>([]);
  const [selected, setSelected] = useState<ProjectState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [, setClock] = useState(0);

  const navigate = useCallback((next: Partial<Route>) => {
    setRoute((current) => {
      const merged = { ...current, ...next };
      const hash = routeHash(merged);
      if (window.location.hash !== hash) window.history.pushState(null, "", hash);
      return merged;
    });
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("popstate", onPop);
    window.addEventListener("hashchange", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("hashchange", onPop);
    };
  }, []);

  // Keep relative timestamps ("3m ago") fresh.
  useEffect(() => {
    const timer = window.setInterval(() => setClock((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api.listRuns());
      setError(null);
      return true;
    } catch (caught) {
      setError(`Cannot reach the ReForge API: ${message(caught)}`);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let attempts = 0;
    let timer: number | undefined;
    const load = async () => {
      if (!(await refreshRuns()) && attempts < 5) {
        attempts += 1;
        timer = window.setTimeout(() => void load(), 1_500);
      }
    };
    void load();
    return () => window.clearTimeout(timer);
  }, [refreshRuns]);

  const loadRun = useCallback(async (runId: string) => {
    try {
      const current = await api.getRun(runId);
      setSelected(current);
      setRuns((items) => {
        const rest = items.filter((item) => item.run_id !== current.run_id);
        return items.some((item) => item.run_id === current.run_id)
          ? items.map((item) => (item.run_id === current.run_id ? current : item))
          : [current, ...rest];
      });
    } catch (caught) {
      setError(message(caught));
    }
  }, []);

  // Load the run named in the URL.
  useEffect(() => {
    if (route.page !== "runs" || !route.runId) {
      setSelected(null);
      return;
    }
    if (selected?.run_id !== route.runId) void loadRun(route.runId);
  }, [route.page, route.runId]);

  // Stream live updates for an in-progress run.
  useEffect(() => {
    if (!selected || selected.final_status) return;
    const runId = selected.run_id;
    const events = new EventSource(api.eventUrl(runId));
    let pending: number | undefined;
    const refresh = () => {
      window.clearTimeout(pending);
      pending = window.setTimeout(() => void loadRun(runId), 250);
    };
    events.addEventListener("trace", refresh);
    events.addEventListener("complete", () => {
      void loadRun(runId);
      void refreshRuns();
      events.close();
    });
    events.onerror = () => events.close();
    return () => {
      window.clearTimeout(pending);
      events.close();
    };
  }, [selected?.run_id, selected?.final_status, loadRun, refreshRuns]);

  const startRun = async (input: NewRunInput): Promise<boolean> => {
    try {
      const created = await api.createRun(input);
      setDialogOpen(false);
      navigate({ page: "runs", runId: created.run_id, tab: "overview" });
      await loadRun(created.run_id);
      return true;
    } catch (caught) {
      setError(`Could not start the run: ${message(caught)}`);
      return false;
    }
  };

  const cancelRun = async () => {
    if (!selected || !window.confirm("Cancel this run? It stops at the next checkpoint.")) return;
    try {
      setSelected(await api.cancelRun(selected.run_id));
      void refreshRuns();
    } catch (caught) {
      setError(message(caught));
    }
  };

  return (
    <div className="app">
      <header className="appbar">
        <a className="appbar-brand" href="#/runs" onClick={() => navigate({ page: "runs", runId: null })}>
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><path d="M4 3h7a5 5 0 0 1 1.6 9.74L16 17h-2.6l-3.1-4H6.5v4H4zm2.5 2.3v5.4H11a2.7 2.7 0 0 0 0-5.4z" fill="currentColor" /></svg>
          ReForge
        </a>
        <nav className="appbar-nav" aria-label="Primary">
          <a href="#/runs" className={route.page === "runs" ? "active" : ""}>Runs</a>
          <a href="#/benchmarks" className={route.page === "benchmarks" ? "active" : ""}>Benchmarks</a>
        </nav>
        <div className="appbar-actions">
          <button type="button" className="button button-primary" onClick={() => setDialogOpen(true)}>New run</button>
        </div>
      </header>

      {error && (
        <div className="banner" role="alert">
          <span>{error}</span>
          <button type="button" className="link-button" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      {route.page === "benchmarks" ? (
        <main className="main main-full"><Benchmarks onError={setError} /></main>
      ) : (
        <div className="workspace">
          <aside className="sidebar" aria-label="Runs">
            <RunList
              runs={runs}
              selectedId={route.runId}
              loading={loading}
              onSelect={(run) => { setSelected(run); navigate({ page: "runs", runId: run.run_id, tab: route.runId === run.run_id ? route.tab : "overview" }); }}
            />
          </aside>
          <main className="main">
            {selected ? (
              <RunDetail state={selected} tab={route.tab} onTab={(tab) => navigate({ tab })} onCancel={() => void cancelRun()} />
            ) : route.runId ? (
              <p className="empty-inline">Loading run…</p>
            ) : (
              <Welcome hasRuns={runs.length > 0} onNew={() => setDialogOpen(true)} />
            )}
          </main>
        </div>
      )}

      <NewRunDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onSubmit={startRun} />
    </div>
  );
}

function RunDetail({ state, tab, onTab, onCancel }: { state: ProjectState; tab: Tab; onTab: (tab: Tab) => void; onCancel: () => void }) {
  const status = runStatus(state);
  const started = startedAt(state);
  return (
    <div className="page">
      <header className="page-header">
        <div className="page-title">
          <div className="page-title-row">
            <h1>{repoName(state)}</h1>
            <StatusPill status={status} />
          </div>
          <p className="page-subtitle">{state.objective}</p>
          <p className="page-meta">
            <span className="mono">{state.run_id.slice(0, 8)}</span>
            <span>Started {relativeTime(started)}</span>
            <span>{duration(runDuration(state))}</span>
            {state.analysis?.languages?.length ? <span>{state.analysis.languages.map(label).join(", ")}</span> : null}
          </p>
        </div>
        <div className="page-actions">
          {!state.final_status && <button type="button" className="button button-danger" onClick={onCancel}>Cancel run</button>}
          {state.report_path && <a className="button" href={api.reportUrl(state.run_id, "markdown")} download={`reforge-${state.run_id.slice(0, 8)}.md`}>Download report</a>}
        </div>
      </header>
      <StageTimeline state={state} />
      <nav className="tabs" aria-label="Run sections">
        {TABS.map(([key, label]) => (
          <button key={key} type="button" className={tab === key ? "active" : ""} aria-current={tab === key ? "page" : undefined} onClick={() => onTab(key)}>
            {label}
            {key === "tests" && state.test_runs.length > 0 && <span className="tab-count">{state.test_runs.length}</span>}
          </button>
        ))}
      </nav>
      <div className="tab-panel">
        {tab === "overview" && <Overview state={state} onOpenTests={() => onTab("tests")} />}
        {tab === "analysis" && <Analysis state={state} />}
        {tab === "plan" && <Plan state={state} />}
        {tab === "changes" && <Changes state={state} />}
        {tab === "tests" && <Tests state={state} />}
        {tab === "activity" && <Activity state={state} />}
        {tab === "report" && <Report state={state} />}
      </div>
    </div>
  );
}

function Welcome({ hasRuns, onNew }: { hasRuns: boolean; onNew: () => void }) {
  return (
    <div className="welcome">
      <h1>{hasRuns ? "Select a run" : "Modernize a repository"}</h1>
      <p className="muted">
        ReForge analyzes a Python or JavaScript/TypeScript repository, freezes its current test behavior, applies a
        modernization plan in a sandbox, and repairs any regressions before reporting.
      </p>
      <ol className="welcome-steps">
        <li><strong>Point it at a repository</strong><span>A Git URL or a ZIP archive.</span></li>
        <li><strong>Optionally set an objective</strong><span>Or let the analysis agent find the work.</span></li>
        <li><strong>Review the result</strong><span>Plan, diff, test comparison, and a final report.</span></li>
      </ol>
      <button type="button" className="button button-primary" onClick={onNew}>New run</button>
    </div>
  );
}
