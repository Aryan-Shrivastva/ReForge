import { useMemo, useState } from "react";
import type { ProjectState } from "../types";
import { clockTime, label } from "../lib/format";

export function Logs({ state }: { state: ProjectState }) {
  const [source, setSource] = useState("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const sources = useMemo(() => [...new Set(state.trace.map((event) => event.agent))], [state.trace]);
  const events = state.trace.filter((event) => (source === "all" || event.agent === source) && (!errorsOnly || event.kind === "error"));

  return (
    <div className="stack">
      <div className="toolbar">
        <select className="input select" value={source} onChange={(event) => setSource(event.target.value)} aria-label="Filter by source">
          <option value="all">All sources</option>
          {sources.map((name) => <option key={name} value={name}>{label(name)}</option>)}
        </select>
        <label className="check">
          <input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} />
          Errors only
        </label>
        <span className="muted toolbar-end">{events.length} of {state.trace.length} events</span>
      </div>
      <div className="logs" role="log">
        {events.length ? events.map((event, index) => (
          <div key={index} className={`log-line log-${event.kind}`}>
            <time dateTime={event.ts}>{clockTime(event.ts)}</time>
            <span className="log-source">{label(event.agent)}</span>
            <span className="log-text">{event.summary}</span>
          </div>
        )) : <div className="log-line"><span className="log-text">No matching events.</span></div>}
      </div>
    </div>
  );
}
