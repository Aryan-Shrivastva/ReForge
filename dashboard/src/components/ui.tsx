import type { ReactNode } from "react";
import type { RunStatus } from "../lib/format";

const STATUS_LABEL: Record<string, string> = {
  SUCCESS: "Succeeded",
  PARTIAL: "Partial",
  FAILED: "Failed",
  ABORTED: "Cancelled",
  RUNNING: "Running",
  QUEUED: "Queued",
};

export function StatusPill({ status }: { status: RunStatus }) {
  return (
    <span className={`pill pill-${status.toLowerCase()}`}>
      <span className="pill-dot" aria-hidden="true" />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** Small neutral label for step, test, and trace states. */
export function Tag({ tone = "neutral", children }: { tone?: "neutral" | "good" | "bad" | "warn" | "info"; children: ReactNode }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function Panel({ title, actions, children, flush = false }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; flush?: boolean }) {
  return (
    <section className={`panel${flush ? " panel-flush" : ""}`}>
      {(title || actions) && (
        <header className="panel-header">
          {title && <h2>{title}</h2>}
          {actions && <div className="panel-actions">{actions}</div>}
        </header>
      )}
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
      {hint && <p className="stat-hint">{hint}</p>}
    </div>
  );
}

export function StatGrid({ children }: { children: ReactNode }) {
  return <dl className="stat-grid">{children}</dl>;
}

export function KeyValues({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="kv">
      {items.map(([key, value]) => (
        <div key={key} className="kv-row">
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function DataTable({ columns, rows, empty = "Nothing to show." }: { columns: string[]; rows: ReactNode[][]; empty?: string }) {
  if (!rows.length) return <p className="empty-inline">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Callout({ tone, title, children }: { tone: "bad" | "warn" | "info"; title: string; children?: ReactNode }) {
  return (
    <div className={`callout callout-${tone}`} role={tone === "bad" ? "alert" : "status"}>
      <strong>{title}</strong>
      {children && <div className="callout-body">{children}</div>}
    </div>
  );
}

export function Waiting({ what }: { what: string }) {
  return <p className="empty-inline">{what} will appear here once the run reaches that stage.</p>;
}

export function Code({ children }: { children: ReactNode }) {
  return <code className="code-inline">{children}</code>;
}
