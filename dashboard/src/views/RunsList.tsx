import { useMemo, useState } from "react";
import type { ProjectState } from "../types";
import { duration, relativeTime, repoName, runDuration, runStatus, updatedAt, type RunStatus } from "../lib/format";
import { Status } from "../components/ui";

type Filter = "all" | "active" | "ready" | "attention";

const FILTERS: Array<[Filter, string]> = [
  ["all", "All"],
  ["active", "Running"],
  ["ready", "Ready"],
  ["attention", "Needs attention"],
];

function matches(filter: Filter, status: RunStatus): boolean {
  if (filter === "active") return status === "RUNNING" || status === "QUEUED";
  if (filter === "ready") return status === "SUCCESS";
  if (filter === "attention") return status === "FAILED" || status === "PARTIAL" || status === "ABORTED";
  return true;
}

export function RunsList({ runs, loading, onOpen, onNew }: { runs: ProjectState[]; loading: boolean; onOpen: (runId: string) => void; onNew: () => void }) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return runs.filter((run) => matches(filter, runStatus(run)) && (!needle || [repoName(run), run.objective, run.run_id].some((value) => value.toLowerCase().includes(needle))));
  }, [runs, query, filter]);

  return (
    <div className="container">
      <header className="page-head">
        <div>
          <h1>Runs</h1>
          <p className="muted">Each run analyzes a repository, records its current behavior, applies modernization changes, and verifies nothing broke.</p>
        </div>
      </header>

      {!loading && runs.length === 0 ? (
        <div className="empty">
          <h2>No runs yet</h2>
          <p className="muted">Point ReForge at a Git repository or a ZIP archive to start.</p>
          <button type="button" className="btn btn-primary" onClick={onNew}>Start your first run</button>
        </div>
      ) : (
        <>
          <div className="toolbar">
            <input type="search" className="input search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search repositories, objectives, or run IDs" aria-label="Search runs" />
            <div className="filters" role="tablist" aria-label="Filter by status">
              {FILTERS.map(([value, label]) => (
                <button key={value} type="button" role="tab" aria-selected={filter === value} className={filter === value ? "active" : ""} onClick={() => setFilter(value)}>{label}</button>
              ))}
            </div>
          </div>
          <div className="list">
            {loading && !runs.length && <p className="list-empty">Loading runs…</p>}
            {!loading && !visible.length && <p className="list-empty">No runs match these filters.</p>}
            {visible.map((run) => (
              <button key={run.run_id} type="button" className="list-row" onClick={() => onOpen(run.run_id)}>
                <span className="list-main">
                  <span className="list-title">{repoName(run)}</span>
                  <span className="list-sub">{run.objective}</span>
                </span>
                <span className="list-status"><Status status={runStatus(run)} /></span>
                <span className="list-meta mono">{run.run_id.slice(0, 7)}</span>
                <span className="list-meta">{duration(runDuration(run))}</span>
                <span className="list-meta list-time">{relativeTime(updatedAt(run))}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
