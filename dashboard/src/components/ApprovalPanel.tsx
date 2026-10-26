import { useMemo, useState } from "react";
import type { ProjectState } from "../types";

export type ReviewDecision =
  | { decision: "approved"; approved_steps: string[] }
  | { decision: "revision_requested"; feedback: string }
  | { decision: "rejected"; feedback: string };

/** The human-in-the-loop gate: review the proposed plan, then approve, revise, or reject it. */
export function ApprovalPanel({ state, onReview }: { state: ProjectState; onReview: (review: ReviewDecision) => Promise<boolean> }) {
  const plan = state.plan;
  const [selected, setSelected] = useState<Set<string>>(() => new Set(plan?.steps.map((step) => step.id) ?? []));
  const [mode, setMode] = useState<"choose" | "revise" | "reject">("choose");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);

  // A step cannot run if anything it depends on is left out.
  const effective = useMemo(() => {
    const included = new Set(selected);
    let changed = true;
    while (changed && plan) {
      changed = false;
      for (const step of plan.steps) {
        if (included.has(step.id) && step.depends_on.some((dependency) => !included.has(dependency))) {
          included.delete(step.id);
          changed = true;
        }
      }
    }
    return included;
  }, [selected, plan]);

  if (!plan) return null;
  const previous = (state.reviews ?? []).filter((review) => review.decision === "revision_requested");

  const toggle = (id: string) => setSelected((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const submit = async (review: ReviewDecision) => {
    setBusy(true);
    const done = await onReview(review);
    setBusy(false);
    if (done) setFeedback("");
  };

  return (
    <section className="approval" aria-label="Review the proposed plan">
      <header className="approval-head">
        <div>
          <h2>Proposed changes · plan v{plan.version}</h2>
          <p className="muted">{plan.rationale}</p>
        </div>
      </header>

      {previous.length > 0 && (
        <p className="approval-note">
          Revised after your feedback: “{previous[previous.length - 1].feedback}”
        </p>
      )}

      <ol className="proposal">
        {plan.steps.map((step) => {
          const checked = selected.has(step.id);
          const blocked = checked && !effective.has(step.id);
          return (
            <li key={step.id} className={`proposal-item${effective.has(step.id) ? "" : " proposal-off"}`}>
              <label className="proposal-check">
                <input type="checkbox" checked={checked} disabled={mode !== "choose" || busy} onChange={() => toggle(step.id)} />
                <span className="proposal-body">
                  <span className="proposal-title"><span className="mono proposal-id">{step.id}</span>{step.title}</span>
                  <span className="muted">{step.description}</span>
                  <span className="proposal-meta">
                    <span className="mono">{step.files.join(", ") || "no files"}</span>
                    {step.depends_on.length > 0 && <span>needs {step.depends_on.join(", ")}</span>}
                    <span>verified by: {step.validation_hint}</span>
                  </span>
                  {blocked && <span className="proposal-warn">Will be skipped: it depends on a change you left out.</span>}
                </span>
              </label>
            </li>
          );
        })}
      </ol>

      {mode === "choose" ? (
        <div className="approval-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || effective.size === 0}
            onClick={() => void submit({ decision: "approved", approved_steps: [...effective] })}
          >
            {busy ? "Submitting…" : effective.size === plan.steps.length ? "Approve and apply" : `Approve ${effective.size} of ${plan.steps.length}`}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => setMode("revise")}>Request changes</button>
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => setMode("reject")}>Reject</button>
          <span className="muted approval-hint">Changes are applied on the branch <span className="mono">{state.work_branch}</span>, never on your main branch.</span>
        </div>
      ) : (
        <div className="approval-feedback">
          <label className="field">
            <span>{mode === "revise" ? "What should the planner change?" : "Reason for rejecting (optional)"}</span>
            <textarea
              className="input"
              rows={3}
              value={feedback}
              autoFocus
              onChange={(event) => setFeedback(event.target.value)}
              placeholder={mode === "revise" ? "e.g. Leave package.json alone for now; only upgrade the deprecated API calls." : "e.g. Not a priority this sprint."}
            />
          </label>
          <div className="approval-actions">
            {mode === "revise" ? (
              <button type="button" className="btn btn-primary" disabled={busy || !feedback.trim()} onClick={() => void submit({ decision: "revision_requested", feedback })}>
                {busy ? "Sending…" : "Send to planner"}
              </button>
            ) : (
              <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void submit({ decision: "rejected", feedback })}>
                {busy ? "Rejecting…" : "Reject plan"}
              </button>
            )}
            <button type="button" className="btn" disabled={busy} onClick={() => setMode("choose")}>Back</button>
          </div>
        </div>
      )}
    </section>
  );
}
