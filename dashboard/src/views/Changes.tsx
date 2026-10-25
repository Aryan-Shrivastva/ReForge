import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProjectState } from "../types";
import { message } from "../lib/format";

interface FileDiff {
  path: string;
  added: number;
  removed: number;
  lines: string[];
}

export function Changes({ state }: { state: ProjectState }) {
  const [step, setStep] = useState("");
  const [diff, setDiff] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const chosen = state.plan?.steps.find((item) => item.id === step);
  const applied = state.plan?.steps.filter((item) => item.status === "applied").length ?? 0;

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
  }, [state.run_id, step, state.final_status, applied]);

  const files = diff ? splitFiles(diff) : [];
  return (
    <div className="stack">
      <div className="toolbar">
        <select className="input select" value={step} onChange={(event) => setStep(event.target.value)} aria-label="Choose which change to show">
          <option value="">All changes</option>
          {state.plan?.steps.map((item) => (
            <option key={item.id} value={item.id}>{item.id} · {item.title}{item.status !== "applied" ? ` (${item.status})` : ""}</option>
          ))}
        </select>
        {files.length > 0 && (
          <span className="muted">
            {files.length} file{files.length === 1 ? "" : "s"} · <span className="add">+{files.reduce((sum, file) => sum + file.added, 0)}</span>{" "}
            <span className="del">−{files.reduce((sum, file) => sum + file.removed, 0)}</span>
          </span>
        )}
      </div>
      {error ? (
        <p className="muted">The diff is not available: {error}</p>
      ) : diff === null ? (
        <p className="muted">Loading…</p>
      ) : !files.length ? (
        <div className="empty empty-sm">
          <p className="muted">{chosen && chosen.status !== "applied" ? `${chosen.id} was ${chosen.status} and changed no files.` : "No code has been changed."}</p>
        </div>
      ) : (
        files.map((file) => (
          <section key={file.path} className="file">
            <header className="file-head">
              <span className="mono">{file.path}</span>
              <span><span className="add">+{file.added}</span> <span className="del">−{file.removed}</span></span>
            </header>
            <pre className="diff">
              {file.lines.map((line, index) => {
                const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
                return <span key={index} className={`diff-line diff-${kind}`}>{line || " "}{"\n"}</span>;
              })}
            </pre>
          </section>
        ))
      )}
    </div>
  );
}

function splitFiles(text: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  for (const line of text.replace(/\n$/, "").split("\n")) {
    if (line.startsWith("diff --git ")) {
      const match = line.match(/ b\/(.+)$/);
      current = { path: match ? match[1] : line, added: 0, removed: 0, lines: [] };
      files.push(current);
      continue;
    }
    if (!current) continue;
    if (/^(index |new file|deleted file|similarity|rename |old mode|new mode)/.test(line) || line.startsWith("--- ") || line.startsWith("+++ ")) continue;
    if (line.startsWith("+")) current.added += 1;
    else if (line.startsWith("-")) current.removed += 1;
    current.lines.push(line);
  }
  return files;
}
