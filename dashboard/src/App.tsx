import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { ProjectState } from "./types";
import { message, repoName } from "./lib/format";
import type { ReviewDecision } from "./components/ApprovalPanel";
import { NewRunDialog, type NewRunInput } from "./components/NewRunDialog";
import { Benchmarks } from "./views/Benchmarks";
import { RUN_TABS, RunPage, type RunTab } from "./views/RunPage";
import { RunsList } from "./views/RunsList";

type Route =
  | { page: "runs" }
  | { page: "run"; runId: string; tab: RunTab }
  | { page: "benchmarks" };

function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "benchmarks") return { page: "benchmarks" };
  if (parts[0] === "runs" && parts[1]) {
    const tab = RUN_TABS.some(([key]) => key === parts[2]) ? (parts[2] as RunTab) : "overview";
    return { page: "run", runId: parts[1], tab };
  }
  return { page: "runs" };
}

function toHash(route: Route): string {
  if (route.page === "benchmarks") return "#/benchmarks";
  if (route.page === "run") return `#/runs/${route.runId}${route.tab === "overview" ? "" : `/${route.tab}`}`;
  return "#/";
}

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [runs, setRuns] = useState<ProjectState[]>([]);
  const [selected, setSelected] = useState<ProjectState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [, setClock] = useState(0);

  const navigate = useCallback((next: Route) => {
    const hash = toHash(next);
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    setRoute(next);
    window.scrollTo({ top: 0 });
  }, []);

  useEffect(() => {
    const sync = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("popstate", sync);
    window.addEventListener("hashchange", sync);
    return () => {
      window.removeEventListener("popstate", sync);
      window.removeEventListener("hashchange", sync);
    };
  }, []);

  // Keep relative times ("3m ago") current.
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
      setError(`Cannot reach the ReForge API. ${message(caught)}`);
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
      setRuns((items) => items.some((item) => item.run_id === runId)
        ? items.map((item) => (item.run_id === runId ? current : item))
        : [current, ...items]);
    } catch (caught) {
      setError(message(caught));
    }
  }, []);

  const routeRunId = route.page === "run" ? route.runId : null;
  useEffect(() => {
    if (!routeRunId) {
      setSelected(null);
      return;
    }
    if (selected?.run_id !== routeRunId) {
      setSelected(runs.find((run) => run.run_id === routeRunId) ?? null);
      void loadRun(routeRunId);
    }
  }, [routeRunId]);

  // Live updates while a run is in progress.
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
      navigate({ page: "run", runId: created.run_id, tab: "overview" });
      await loadRun(created.run_id);
      return true;
    } catch (caught) {
      setError(`Could not start the run. ${message(caught)}`);
      return false;
    }
  };

  const cancelRun = async () => {
    if (!selected || !window.confirm("Cancel this run? It stops at its next checkpoint.")) return;
    try {
      setSelected(await api.cancelRun(selected.run_id));
      void refreshRuns();
    } catch (caught) {
      setError(message(caught));
    }
  };

  const rerun = async (run: ProjectState) => {
    try {
      const created = await api.rerun(run.run_id);
      await refreshRuns();
      navigate({ page: "run", runId: created.run_id, tab: "overview" });
      await loadRun(created.run_id);
    } catch (caught) {
      setError(`Could not rerun this project. ${message(caught)}`);
    }
  };

  const reviewPlan = async (run: ProjectState, review: ReviewDecision): Promise<boolean> => {
    try {
      setSelected(await api.reviewPlan(run.run_id, review));
      void refreshRuns();
      return true;
    } catch (caught) {
      setError(`Could not submit your decision. ${message(caught)}`);
      return false;
    }
  };

  const deleteRun = async (run: ProjectState) => {
    if (!run.final_status) return;
    if (!window.confirm(`Delete run ${run.run_id.slice(0, 7)} for ${repoName(run)}? Its report and run files will be permanently removed.`)) return;
    try {
      await api.deleteRun(run.run_id);
      setRuns((items) => items.filter((item) => item.run_id !== run.run_id));
      if (selected?.run_id === run.run_id) {
        setSelected(null);
        navigate({ page: "runs" });
      }
    } catch (caught) {
      setError(`Could not delete this run. ${message(caught)}`);
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <nav className="crumbs" aria-label="Breadcrumb">
            <a href="#/" className="brand" onClick={(event) => { event.preventDefault(); navigate({ page: "runs" }); }}>
              <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M12 2 22 20H2z" fill="currentColor" /></svg>
              ReForge
            </a>
            {route.page === "run" && (
              <>
                <span className="crumb-sep" aria-hidden="true">/</span>
                <span className="crumb">{selected ? repoName(selected) : "Run"}</span>
              </>
            )}
          </nav>
          <nav className="topnav" aria-label="Primary">
            <a href="#/" className={route.page !== "benchmarks" ? "active" : ""} onClick={(event) => { event.preventDefault(); navigate({ page: "runs" }); }}>Runs</a>
            <a href="#/benchmarks" className={route.page === "benchmarks" ? "active" : ""} onClick={(event) => { event.preventDefault(); navigate({ page: "benchmarks" }); }}>Benchmarks</a>
          </nav>
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setDialogOpen(true)}>New run</button>
        </div>
      </header>

      {error && (
        <div className="banner" role="alert">
          <div className="banner-inner">
            <span>{error}</span>
            <button type="button" className="link" onClick={() => setError(null)}>Dismiss</button>
          </div>
        </div>
      )}

      <main className="main">
        {route.page === "benchmarks" && <Benchmarks onError={setError} />}
        {route.page === "runs" && (
          <RunsList
            runs={runs}
            loading={loading}
            onOpen={(runId) => navigate({ page: "run", runId, tab: "overview" })}
            onNew={() => setDialogOpen(true)}
            onDelete={(run) => void deleteRun(run)}
            onRerun={(run) => void rerun(run)}
          />
        )}
        {route.page === "run" && (
          selected
            ? <RunPage
                state={selected}
                tab={route.tab}
                onTab={(tab) => navigate({ page: "run", runId: route.runId, tab })}
                onCancel={() => void cancelRun()}
                onDelete={() => void deleteRun(selected)}
                onRerun={() => void rerun(selected)}
                onReview={(review) => reviewPlan(selected, review)}
              />
            : <div className="container"><p className="muted">Loading run…</p></div>
        )}
      </main>

      <NewRunDialog open={dialogOpen} onClose={() => setDialogOpen(false)} onSubmit={startRun} />
    </div>
  );
}
