import { useMemo, useState } from "react";
import type { ProjectState, TraceEvent } from "../types";
import { clockTime, label } from "../lib/format";
import { Panel, Tag } from "../components/ui";

const KIND_TONE: Record<TraceEvent["kind"], "good" | "bad" | "warn" | "info" | "neutral"> = {
  start: "neutral",
  tool_call: "neutral",
  result: "good",
  decision: "info",
  error: "bad",
};

export function Activity({ state }: { state: ProjectState }) {
  const [agent, setAgent] = useState("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const agents = useMemo(() => [...new Set(state.trace.map((event) => event.agent))], [state.trace]);
  const events = state.trace
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => (agent === "all" || event.agent === agent) && (!errorsOnly || event.kind === "error"))
    .reverse();

  return (
    <Panel
      title={`Activity (${state.trace.length})`}
      flush
      actions={
        <>
          <label className="checkbox">
            <input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} />
            Errors only
          </label>
          <select value={agent} onChange={(event) => setAgent(event.target.value)} aria-label="Filter by agent">
            <option value="all">All sources</option>
            {agents.map((name) => <option key={name} value={name}>{label(name)}</option>)}
          </select>
        </>
      }
    >
      {events.length ? (
        <ol className="activity">
          {events.map(({ event, index }) => (
            <li key={index} className={`activity-item${event.kind === "error" ? " activity-error" : ""}`}>
              <time dateTime={event.ts}>{clockTime(event.ts)}</time>
              <span className="activity-source">{label(event.agent)}</span>
              <Tag tone={KIND_TONE[event.kind]}>{label(event.kind)}</Tag>
              <span className="activity-summary">{event.summary}</span>
              <span className="activity-stage">{label(event.stage.toLowerCase())}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="empty-inline">No matching events.</p>
      )}
    </Panel>
  );
}
