import { useMemo, useState } from "react";
import type { ProjectState } from "../types";
import { relativeTime, repoName, runStatus, updatedAt, type RunStatus } from "../lib/format";
import { StatusPill } from "./ui";

type Filter = "all" | "active" | "SUCCESS" | "PARTIAL" | "FAILED";

const FILTERS: Array<[Filter, string]> = [
  ["all", "All"],
  ["active", "Active"],
  ["SUCCESS", "Succeeded"],
  ["PARTIAL", "Partial"],
  ["FAILED", "Failed"],
];

function matches(filter: Filter, status: RunStatus): boolean {
  if (filter === "all") return true;
  if (filter === "active") return status === "RUNNING" || status === "QUEUED";
  if (filter === "FAILED") return status === "FAILED" || status === "ABORTED";
  return status === filter;
}

export function RunList({
  runs,
  selectedId,
  loading,
  onSelect,
}: {
  runs: ProjectState[];
  selectedId: string | null;
  loading: boolean;
  onSelect: (run: ProjectState) => void;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return runs.filter((run) => {
      if (!matches(filter, runStatus(run))) return false;
      if (!needle) return true;
      return [repoName(run), run.objective, run.run_id].some((value) => value.toLowerCase().includes(needle));
    });
  }, [runs, query, filter]);

  return (
    <div className="run-list">
      <div className="run-list-controls">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search runs"
          aria-label="Search runs"
        />
        <div className="segmented" role="tablist" aria-label="Filter runs by status">
          {FILTERS.map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={filter === value}
              className={filter === value ? "active" : ""}
              onClick={() => setFilter(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
      <ul className="run-items">
        {loading && !runs.length && <li className="run-placeholder">Loading runs…</li>}
        {!loading && !visible.length && (
          <li className="run-placeholder">{runs.length ? "No runs match this filter." : "No runs yet."}</li>
        )}
        {visible.map((run) => (
          <li key={run.run_id}>
            <button
              type="button"
              className={`run-item${run.run_id === selectedId ? " selected" : ""}`}
              onClick={() => onSelect(run)}
            >
              <span className="run-item-top">
                <span className="run-item-name">{repoName(run)}</span>
                <span className="run-item-time">{relativeTime(updatedAt(run))}</span>
              </span>
              <span className="run-item-bottom">
                <StatusPill status={runStatus(run)} />
                <span className="run-item-id">{run.run_id.slice(0, 8)}</span>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
