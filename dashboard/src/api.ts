import type { Benchmark, EvaluationSummary, ProjectState } from "./types";

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase}${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listRuns: () => request<ProjectState[]>("/runs"),
  getRun: (runId: string) => request<ProjectState>(`/runs/${encodeURIComponent(runId)}`),
  getDiff: async (runId: string, step?: string) => {
    const suffix = step ? `?step=${encodeURIComponent(step)}` : "";
    const response = await fetch(`${apiBase}/runs/${encodeURIComponent(runId)}/diff${suffix}`);
    if (!response.ok) throw new Error(await response.text());
    return response.text();
  },
  getReport: async (runId: string) => {
    const response = await fetch(`${apiBase}/runs/${encodeURIComponent(runId)}/report`);
    if (!response.ok) throw new Error(await response.text());
    return response.text();
  },
  reportUrl: (runId: string, format: "markdown" | "json") =>
    `${apiBase}/runs/${encodeURIComponent(runId)}/report?format=${format}`,
  listBenchmarks: () => request<Benchmark[]>("/benchmarks"),
  runBenchmarks: (payload: { names?: string[]; repetitions: number; ablation: string }) =>
    request<EvaluationSummary>("/benchmarks/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }),
  cancelRun: (runId: string) =>
    request<ProjectState>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }),
  createRun: async (input: { objective?: string; repoUrl?: string; upload?: File }) => {
    if (input.upload) {
      const body = new FormData();
      if (input.objective?.trim()) body.set("objective", input.objective.trim());
      body.set("upload", input.upload);
      return request<{ run_id: string }>("/runs", { method: "POST", body });
    }
    return request<{ run_id: string }>("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...(input.objective?.trim() ? { objective: input.objective.trim() } : {}), repo_url: input.repoUrl })
    });
  },
  eventUrl: (runId: string) => `${apiBase}/runs/${encodeURIComponent(runId)}/events`
};
