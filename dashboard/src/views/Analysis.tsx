import type { ProjectState } from "../types";
import { titleCase } from "../lib/format";
import { Code, DataTable, Panel, Tag, Waiting } from "../components/ui";

const DEPENDENCY_TONE: Record<string, "good" | "bad" | "warn" | "neutral"> = {
  ok: "good",
  outdated: "warn",
  unpinned: "neutral",
  deprecated: "bad",
  incompatible: "bad",
};

export function Analysis({ state }: { state: ProjectState }) {
  const analysis = state.analysis;
  if (!analysis) return <Panel title="Analysis"><Waiting what="The repository analysis" /></Panel>;
  return (
    <div className="view-stack">
      <Panel title="Summary">
        <p className="prose">{analysis.summary}</p>
        {analysis.risks.length > 0 && (
          <>
            <h3 className="subheading">Risks</h3>
            <ul className="plain-list">{analysis.risks.map((risk) => <li key={risk}>{risk}</li>)}</ul>
          </>
        )}
      </Panel>
      <Panel title={`Dependencies (${analysis.dependencies.length})`} flush>
        <DataTable
          columns={["Package", "Current", "Target", "Status", "Notes"]}
          empty="No dependency declarations were found."
          rows={analysis.dependencies.map((item) => [
            <span className="mono">{item.name}</span>,
            <span className="mono">{item.current ?? "—"}</span>,
            <span className="mono">{item.target ?? "—"}</span>,
            <Tag tone={DEPENDENCY_TONE[item.status] ?? "neutral"}>{titleCase(item.status)}</Tag>,
            item.reason,
          ])}
        />
      </Panel>
      <Panel title={`Deprecated patterns (${analysis.deprecated_patterns.length})`} flush>
        <DataTable
          columns={["Rule", "Location", "Code", "Suggested replacement"]}
          empty="No catalogued deprecated patterns were found."
          rows={analysis.deprecated_patterns.map((item) => [
            <span className="mono">{item.rule_id}</span>,
            <span className="mono">{item.file}:{item.line}</span>,
            <Code>{item.snippet}</Code>,
            item.replacement_hint,
          ])}
        />
      </Panel>
    </div>
  );
}
