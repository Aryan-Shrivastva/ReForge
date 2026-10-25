import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProjectState } from "../types";
import { message } from "../lib/format";
import { Panel } from "../components/ui";

export function Changes({ state }: { state: ProjectState }) {
  const [step, setStep] = useState("");
  const [diff, setDiff] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chosen = state.plan?.steps.find((item) => item.id === step);
  const appliedCount = state.plan?.steps.filter((item) => item.status === "applied").length ?? 0;

  useEffect(() => {
    let active = true;
    setDiff(null);
    setError(null);
    if (chosen && chosen.status !== "applied") {
      setDiff("");
      return;
    }
    api.getDiff(state.run_id, step || undefined)
      .then((text) => { if (active) setDiff(text); })
      .catch((caught) => { if (active) setError(message(caught)); });
    return () => { active = false; };
  }, [state.run_id, step, state.final_status, appliedCount]);

  return (
    <Panel
      title="Changes"
      flush
      actions={
        <select value={step} onChange={(event) => setStep(event.target.value)} aria-label="Choose which change to show">
          <option value="">All changes</option>
          {state.plan?.steps.map((item) => (
            <option key={item.id} value={item.id}>{item.id} · {item.title}{item.status !== "applied" ? ` (${item.status})` : ""}</option>
          ))}
        </select>
      }
    >
      {error ? (
        <p className="empty-inline">The diff is not available: {error}</p>
      ) : diff === null ? (
        <p className="empty-inline">Loading diff…</p>
      ) : !diff.trim() ? (
        <p className="empty-inline">
          {chosen && chosen.status !== "applied" ? `${chosen.id} was ${chosen.status}; it changed no files.` : "No code changes yet."}
        </p>
      ) : (
        <DiffView text={diff} />
      )}
    </Panel>
  );
}

function DiffView({ text }: { text: string }) {
  const lines = text.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff">
      {lines.map((line, index) => {
        let kind = "ctx";
        if (line.startsWith("diff --git") || line.startsWith("index ")) kind = "meta";
        else if (line.startsWith("+++") || line.startsWith("---")) kind = "file";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "del";
        return <span key={index} className={`diff-line diff-${kind}`}>{line || " "}{"\n"}</span>;
      })}
    </pre>
  );
}
