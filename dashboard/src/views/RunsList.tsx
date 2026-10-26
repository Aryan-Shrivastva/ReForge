import { useMemo, useState } from "react";
import type { ProjectState } from "../types";
import { duration, relativeTime, repoName, runDuration, runStatus, updatedAt, type RunStatus } from "../lib/format";
import { Status } from "../components/ui";

type Filter = "all" | "active" | "ready" | "attention";

function projectKey(run: ProjectState): string {
  if (run.project_key) return run.project_key;
  const source = run.repo_source.trim().replace(/\.git$/, "").replace(/[\\/]+$/, "");
  if (/[\\/]upload\.zip$/i.test(source)) return `legacy-upload:${run.run_id}`;
  const repository = source.match(/(?:^|[:/])([^/:]+\/[^/:]+)$/);
  return repository ? repository[1].toLowerCase() : source.toLowerCase() || repoName(run).toLowerCase();
}

const FILTERS: Array<[Filter, string]> = [
  ["all", "All"],
  ["active", "Running"],
  ["ready", "Ready"],
  ["attention", "Needs attention"],
];

function matches(filter: Filter, status: RunStatus): boolean {
  if (filter === "active") return status === "RUNNING" || status === "QUEUED";
  if (filter === "ready") return status === "SUCCESS";
  if (filter === "attention") return status === "AWAITING" || status === "FAILED" || status === "PARTIAL" || status === "ABORTED";
  return true;
}

export function RunsList({ runs, loading, onOpen, onNew, onDelete, onRerun }: {
  runs: ProjectState[];
  loading: boolean;
  onOpen: (runId: string) => void;
  onNew: () => void;
  onDelete: (run: ProjectState) => void;
  onRerun: (run: ProjectState) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const grouped = new Map<string, ProjectState[]>();
    for (const run of runs) {
      const key = projectKey(run);
      grouped.set(key, [...(grouped.get(key) ?? []), run]);
    }
    return [...grouped.entries()]
      .map(([key, items]) => ({ key, runs: items, latest: items[0]! }))
      .filter(({ runs: items }) => items.some((run) => matches(filter, runStatus(run)) && (!needle || [repoName(run), run.project_name ?? "", run.objective, run.run_id].some((value) => value.toLowerCase().includes(needle)))))
      .sort((a, b) => (updatedAt(b.latest)?.getTime() ?? 0) - (updatedAt(a.latest)?.getTime() ?? 0));
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
            {!loading && !groups.length && <p className="list-empty">No runs match these filters.</p>}
            {groups.map(({ key, runs: items, latest }) => (
              <details key={key} className="project-group">
                <summary className="project-summary">
                  <span className="project-heading">
                    <span className="list-title">{repoName(latest)}</span>
                    <span className="list-sub">{items.length} {items.length === 1 ? "run" : "runs"} · latest {relativeTime(updatedAt(latest))}</span>
                  </span>
                  <span className="list-status"><Status status={runStatus(latest)} /></span>
                  <span className="project-latest-objective">{latest.objective}</span>
                  <span className="project-chevron" aria-hidden="true">⌄</span>
                </summary>
                <div className="project-history">
                  {items.map((run) => (
                    <div className="project-run" key={run.run_id}>
                      <button type="button" className="project-run-open" onClick={() => onOpen(run.run_id)}>
                        <span className="list-status"><Status status={runStatus(run)} /></span>
                        <span className="project-run-id mono">{run.run_id.slice(0, 7)}</span>
                        <span className="project-run-objective">{run.objective}</span>
                        <span className="list-meta">{duration(runDuration(run))}</span>
                        <span className="list-meta list-time">{relativeTime(updatedAt(run))}</span>
                      </button>
                      <div className="project-run-actions">
                        <button type="button" className="btn btn-sm" onClick={() => onRerun(run)}>Rerun</button>
                        {run.final_status && <button type="button" className="btn btn-sm btn-danger" onClick={() => onDelete(run)}>Delete</button>}
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
