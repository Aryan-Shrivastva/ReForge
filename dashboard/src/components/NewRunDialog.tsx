import { useEffect, useRef, useState, type FormEvent } from "react";

export interface NewRunInput {
  objective?: string;
  repoUrl?: string;
  upload?: File;
}

export function NewRunDialog({ open, onClose, onSubmit }: { open: boolean; onClose: () => void; onSubmit: (input: NewRunInput) => Promise<boolean> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [source, setSource] = useState<"git" | "zip">("git");
  const [repoUrl, setRepoUrl] = useState("");
  const [upload, setUpload] = useState<File | undefined>();
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  }, [open]);

  const ready = source === "git" ? repoUrl.trim().length > 0 : Boolean(upload);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    const started = await onSubmit({
      objective: objective.trim() || undefined,
      repoUrl: source === "git" ? repoUrl.trim() : undefined,
      upload: source === "zip" ? upload : undefined,
    });
    setBusy(false);
    if (started) {
      setRepoUrl("");
      setUpload(undefined);
      setObjective("");
    }
  };

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose} onCancel={onClose}>
      <form onSubmit={submit}>
        <header className="dialog-header">
          <h2>New modernization run</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">×</button>
        </header>
        <div className="dialog-body">
          <fieldset className="field">
            <legend>Repository source</legend>
            <div className="filters">
              <button type="button" className={source === "git" ? "active" : ""} onClick={() => setSource("git")}>Git URL</button>
              <button type="button" className={source === "zip" ? "active" : ""} onClick={() => setSource("zip")}>ZIP archive</button>
            </div>
          </fieldset>
          {source === "git" ? (
            <label className="field">
              <span>Repository URL</span>
              <input
                className="input"
                value={repoUrl}
                onChange={(event) => setRepoUrl(event.target.value)}
                placeholder="https://github.com/org/repository.git"
                autoFocus
                required
              />
              <small>HTTPS, SSH, or git@host:path. Public repositories, or private ones the server can access.</small>
            </label>
          ) : (
            <label className="field">
              <span>ZIP archive</span>
              <input className="input" type="file" accept=".zip" onChange={(event) => setUpload(event.target.files?.[0])} required />
              <small>The project root, or a single top-level folder such as GitHub's "Download ZIP".</small>
            </label>
          )}
          <label className="field">
            <span>Objective <em>optional</em></span>
            <textarea
              className="input"
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              rows={3}
              placeholder="e.g. Upgrade Express 4 to 5 and replace the deprecated request package"
            />
            <small>Leave blank to let the analysis agent find the highest-confidence modernization work.</small>
          </label>
        </div>
        <footer className="dialog-footer">
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button type="submit" className="btn btn-primary" disabled={!ready || busy}>
            {busy ? "Starting…" : "Start run"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}
