import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProjectState } from "../types";
import { message } from "../lib/format";

export function Report({ state }: { state: ProjectState }) {
  const [report, setReport] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!state.report_path) return;
    let active = true;
    setReport(null);
    setError(null);
    api.getReport(state.run_id)
      .then((text) => { if (active) setReport(text); })
      .catch((caught) => { if (active) setError(message(caught)); });
    return () => { active = false; };
  }, [state.run_id, state.report_path]);

  if (!state.report_path) {
    return <div className="empty empty-sm"><p className="muted">{state.final_status ? "No report was written for this run." : "The report is written when the run finishes."}</p></div>;
  }
  return (
    <div className="stack">
      <div className="toolbar">
        <a className="btn btn-sm" href={api.reportUrl(state.run_id, "markdown")} download={`reforge-${state.run_id.slice(0, 8)}.md`}>Download Markdown</a>
        <a className="btn btn-sm" href={api.reportUrl(state.run_id, "json")} download={`reforge-${state.run_id.slice(0, 8)}.json`}>Download JSON</a>
      </div>
      {error ? <p className="muted">The report could not be loaded: {error}</p> : report === null ? <p className="muted">Loading…</p> : <pre className="report">{report}</pre>}
    </div>
  );
}
