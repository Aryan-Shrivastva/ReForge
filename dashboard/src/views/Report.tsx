import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProjectState } from "../types";
import { message } from "../lib/format";
import { Panel, Waiting } from "../components/ui";

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
    return <Panel title="Report">{state.final_status ? <p className="empty-inline">No report was written for this run.</p> : <Waiting what="The final report" />}</Panel>;
  }
  return (
    <Panel
      title="Final report"
      flush
      actions={
        <>
          <a className="button" href={api.reportUrl(state.run_id, "markdown")} download={`reforge-${state.run_id.slice(0, 8)}.md`}>Markdown</a>
          <a className="button" href={api.reportUrl(state.run_id, "json")} download={`reforge-${state.run_id.slice(0, 8)}.json`}>JSON</a>
        </>
      }
    >
      {error ? <p className="empty-inline">The report could not be loaded: {error}</p> : report === null ? <p className="empty-inline">Loading report…</p> : <pre className="report">{report}</pre>}
    </Panel>
  );
}
