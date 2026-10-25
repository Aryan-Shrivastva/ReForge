import { useEffect, useState } from "react";
import { api } from "../api";
import type { Benchmark, EvaluationSummary } from "../types";
import { duration, message, percent } from "../lib/format";
import { Chip, Meta } from "../components/ui";

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
    <div className="container">
      <header className="page-head">
        <div>
          <h1>Benchmarks</h1>
          <p className="muted">Controlled repositories with known deprecations. Running the suite measures success rate and behavior preservation.</p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => void run()} disabled={busy}>{busy ? "Running suite…" : "Run suite (3×)"}</button>
      </header>
      {summary && (
        <Meta
          items={[
            ["Success rate", percent(summary.success_rate)],
            ["Partial rate", percent(summary.partial_rate)],
            ["Runs", String(summary.runs.length)],
          ]}
        />
      )}
      <div className="list">
        {benchmarks.map((item) => {
          const results = summary?.runs.filter((run) => run.benchmark === item.name) ?? [];
          return (
            <div key={item.name} className="list-row list-row-static">
              <span className="list-main">
                <span className="list-title mono">{item.name}</span>
                <span className="list-sub">{item.objective}</span>
              </span>
              <span className="list-status"><Chip tone={item.has_tests ? "good" : "warn"}>{item.has_tests ? "Has tests" : "No tests"}</Chip></span>
              <span className="list-meta">{results.length ? `${results.filter((run) => run.status === "SUCCESS").length}/${results.length} ready` : ""}</span>
              <span className="list-meta">{results.length ? duration(results.reduce((sum, run) => sum + run.wall_clock_s, 0) / results.length) : ""}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
