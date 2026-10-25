import type { FinalStatus, ProjectState } from "../types";

export type RunStatus = FinalStatus | "RUNNING" | "QUEUED";

/** Human name for a run: "owner/repo" for Git URLs, the archive name for uploads. */
export function repoName(state: ProjectState): string {
  const source = state.repo_source.trim();
  if (!source) return "Untitled run";
  if (/[\\/]upload\.zip$/.test(source)) return state.project_name || "Uploaded archive";
  const cleaned = source.replace(/\.git$/, "").replace(/[\\/]+$/, "");
  const match = cleaned.match(/[:/]([^/:]+)\/([^/]+)$/);
  if (match) return `${match[1]}/${match[2]}`;
  return cleaned.split(/[\\/]/).pop() || cleaned;
}

export function isLegacyFalseSuccess(state: ProjectState): boolean {
  return state.final_status === "SUCCESS" && Boolean(state.plan?.steps.some((step) => step.status !== "applied"));
}

export function runStatus(state: ProjectState): RunStatus {
  if (isLegacyFalseSuccess(state)) return "PARTIAL";
  if (state.final_status) return state.final_status;
  return state.stage === "INIT" && state.trace.length <= 1 ? "QUEUED" : "RUNNING";
}

export function startedAt(state: ProjectState): Date | null {
  return state.trace.length ? new Date(state.trace[0].ts) : null;
}

export function updatedAt(state: ProjectState): Date | null {
  return state.trace.length ? new Date(state.trace[state.trace.length - 1].ts) : null;
}

export function relativeTime(date: Date | null, now: number = Date.now()): string {
  if (!date) return "—";
  const seconds = Math.round((now - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return minutes < 60 ? `${minutes}m ${rest}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function runDuration(state: ProjectState): number {
  if (state.metrics.wall_clock_s > 0) return state.metrics.wall_clock_s;
  const start = startedAt(state);
  const end = updatedAt(state);
  return start && end ? (end.getTime() - start.getTime()) / 1000 : 0;
}

// Fixed locale: compact notation in some locales renders 500000 as "5L" (lakh).
const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const STANDARD = new Intl.NumberFormat("en-US");

export function compactNumber(value: number): string {
  return value >= 10_000 ? COMPACT.format(value) : STANDARD.format(value);
}

const LABELS: Record<string, string> = {
  javascript: "JavaScript",
  typescript: "TypeScript",
  python: "Python",
  java: "Java",
  kotlin: "Kotlin",
  api_replacement: "API replacement",
  test_generation: "Test generation",
  tool_call: "Tool call",
  api: "API",
};

/** Display label for enum-like identifiers (languages, step kinds, agents, event kinds). */
export function label(value: string): string {
  return LABELS[value] ?? titleCase(value);
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function message(value: unknown): string {
  if (value instanceof Error) {
    try {
      const parsed = JSON.parse(value.message) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // Not a JSON error body.
    }
    return value.message;
  }
  return String(value);
}

export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function titleCase(value: string): string {
  return value.replaceAll("_", " ").replace(/^\w/, (letter) => letter.toUpperCase());
}
