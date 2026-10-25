import { useEffect, useState } from "react";
import { api } from "../api";
import type { Benchmark, EvaluationSummary } from "../types";
import { duration, message, percent } from "../lib/format";
import { DataTable, Panel, Stat, StatGrid, Tag } from "../components/ui";

export function Benchmarks({ onError }: { onError: (value: string) => void }) {
  const [benchmarks, setBenchmarks] = useState<Benchmark[]>([]);
  const [summary, setSummary] = useState<EvaluationSummary | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.listBenchmarks().then(setBenchmarks).catch((caught) => onError(message(caught)));
  }, []);

  const run = async () => {
    setBusy(true);
    try {
      setSummary(await api.runBenchmarks({ repetitions: 3, ablation: "iterative" }));
    } catch (caught) {
      onError(message(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1>Benchmarks</h1>
          <p className="muted">Controlled fixtures with known deprecations, used to measure success rate and behavior preservation.</p>
        </div>
        <button type="button" className="button button-primary" onClick={() => void run()} disabled={busy}>
          {busy ? "Running suite…" : "Run suite (3×)"}
        </button>
      </header>
      <div className="view-stack">
        <Panel title={`Fixtures (${benchmarks.length})`} flush>
          <DataTable
            columns={["Benchmark", "Existing tests", "Objective"]}
            empty="No benchmarks found."
            rows={benchmarks.map((item) => [
              <span className="mono">{item.name}</span>,
              <Tag tone={item.has_tests ? "good" : "warn"}>{item.has_tests ? "Yes" : "No"}</Tag>,
              item.objective,
            ])}
          />
        </Panel>
        {summary && (
          <>
            <StatGrid>
              <Stat label="Success rate" value={percent(summary.success_rate)} />
              <Stat label="Partial rate" value={percent(summary.partial_rate)} />
              <Stat label="Runs" value={summary.runs.length} />
            </StatGrid>
            <Panel title="Results" flush>
              <DataTable
                columns={["Benchmark", "Status", "Behavior preserved", "Repairs", "Replans", "Regressions", "Duration"]}
                rows={summary.runs.map((item) => [
                  <span className="mono">{item.benchmark}</span>,
                  item.status ?? "—",
                  percent(item.behavior_preservation),
                  item.repair_iterations,
                  item.replans,
                  item.regressions_introduced,
                  duration(item.wall_clock_s),
                ])}
              />
              {summary.artifact_path && <p className="table-footnote">Saved to <span className="mono">{summary.artifact_path}</span></p>}
            </Panel>
          </>
        )}
      </div>
    </div>
  );
}
