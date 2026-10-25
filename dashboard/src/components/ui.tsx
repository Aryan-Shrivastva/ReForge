import type { ReactNode } from "react";
import type { RunStatus } from "../lib/format";
import type { StepStatus } from "../lib/steps";

const STATUS_TEXT: Record<RunStatus, string> = {
  SUCCESS: "Ready",
  PARTIAL: "Partial",
  FAILED: "Failed",
  ABORTED: "Cancelled",
  RUNNING: "Running",
  QUEUED: "Queued",
};

/** Deployment-style status: a coloured dot and a word. */
export function Status({ status, size = "md" }: { status: RunStatus; size?: "md" | "lg" }) {
  return (
    <span className={`status status-${status.toLowerCase()} status-${size}`}>
      <span className="status-dot" aria-hidden="true" />
      {STATUS_TEXT[status]}
    </span>
  );
}

export function StepIcon({ status }: { status: StepStatus }) {
  if (status === "running") return <span className="step-icon step-icon-running" aria-label="Running" />;
  const paths: Record<Exclude<StepStatus, "running">, ReactNode> = {
    done: <path d="M4.5 8.2 7 10.6l4.6-5.2" />,
    failed: <path d="m5.2 5.2 5.6 5.6m0-5.6-5.6 5.6" />,
    warning: <path d="M8 4.6v4.2m0 2.4v.2" />,
    pending: null,
    skipped: <path d="M5 8h6" />,
  };
  return (
    <svg className={`step-icon step-icon-${status}`} viewBox="0 0 16 16" width="16" height="16" aria-label={status}>
      <circle cx="8" cy="8" r="7.25" />
      {paths[status]}
    </svg>
  );
}

export function Chip({ tone = "neutral", children }: { tone?: "neutral" | "good" | "bad" | "warn" | "info"; children: ReactNode }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="section">
      <header className="section-header">
        <h2>{title}</h2>
        {aside}
      </header>
      {children}
    </section>
  );
}

export function Meta({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="meta">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>;
}
