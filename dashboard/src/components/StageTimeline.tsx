import type { ProjectState } from "../types";
import { runStatus } from "../lib/format";

/** The main path; REPAIR and REPLAN are loops around TEST, shown as counters. */
const PATH = [
  ["ANALYZE", "Analyze"],
  ["PLAN", "Plan"],
  ["BASELINE", "Baseline"],
  ["TRANSFORM", "Transform"],
  ["TEST", "Verify"],
  ["REPORT", "Report"],
] as const;

const POSITION: Record<string, number> = {
  INIT: -1, ANALYZE: 0, PLAN: 1, BASELINE: 2, TRANSFORM: 3, TEST: 4, REPAIR: 4, REPLAN: 3, REPORT: 5, DONE: 6,
};

export function StageTimeline({ state }: { state: ProjectState }) {
  const status = runStatus(state);
  const finished = Boolean(state.final_status);
  // For a finished run, the furthest stage reached is the one before REPORT.
  const reached = finished ? furthestStage(state) : POSITION[state.stage] ?? -1;
  const failedAt = finished && status !== "SUCCESS" ? reached : -1;

  return (
    <ol className="timeline" aria-label="Workflow progress">
      {PATH.map(([key, label], index) => {
        let tone = "todo";
        if (finished) {
          if (index < reached || (status === "SUCCESS" && index <= 5)) tone = "done";
          if (index === failedAt) tone = status === "PARTIAL" ? "warn" : "bad";
          if (key === "REPORT" && state.report_path) tone = "done";
        } else if (index < reached) {
          tone = "done";
        } else if (index === reached) {
          tone = "current";
        }
        const loops = key === "TEST" ? loopLabel(state) : null;
        return (
          <li key={key} className={`timeline-step timeline-${tone}`}>
            <span className="timeline-marker" aria-hidden="true" />
            <span className="timeline-label">{label}</span>
            {loops && <span className="timeline-loops">{loops}</span>}
          </li>
        );
      })}
    </ol>
  );
}

function loopLabel(state: ProjectState): string | null {
  const parts: string[] = [];
  if (state.repairs.length) parts.push(`${state.repairs.length} repair${state.repairs.length === 1 ? "" : "s"}`);
  if (state.replans) parts.push(`${state.replans} replan${state.replans === 1 ? "" : "s"}`);
  return parts.length ? parts.join(" · ") : null;
}

/** The last working stage the orchestrator transitioned into (REPORT/DONE excluded). */
function furthestStage(state: ProjectState): number {
  let furthest = 0;
  for (const event of state.trace) {
    const target = event.data.to;
    if (typeof target !== "string" || target === "REPORT" || target === "DONE") continue;
    furthest = Math.max(furthest, POSITION[target] ?? 0);
  }
  return Math.min(furthest, 4);
}
