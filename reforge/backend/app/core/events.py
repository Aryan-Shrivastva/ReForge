"""Run-state persistence and deterministic trace recording."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from .state import ProjectState, Stage, TraceEvent


class RunStateStore:
    """Persist one validated project state after every workflow event."""

    def __init__(self, runs_root: Path = Path("runs")) -> None:
        self.runs_root = runs_root.resolve()

    def path_for(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id must be a single non-empty path component")
        return self.runs_root / run_id / "state.json"

    def save(self, state: ProjectState) -> Path:
        path = self.path_for(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        # A just-closed JSON file can briefly remain locked by Windows indexing
        # or antivirus. Retrying preserves atomic replacement without silently
        # falling back to an in-place write.
        for attempt in range(3):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                sleep(0.05)
        return path

    def load(self, run_id: str) -> ProjectState:
        path = self.path_for(run_id)
        for attempt in range(3):
            try:
                return ProjectState.model_validate_json(path.read_text(encoding="utf-8"))
            except PermissionError:
                if attempt == 2:
                    raise
                sleep(0.05)
        raise AssertionError("state read retry loop should return or raise")  # pragma: no cover

    def list(self) -> list[ProjectState]:
        """Return persisted runs newest first, ignoring incomplete atomic-write files."""
        if not self.runs_root.is_dir():
            return []
        states: list[ProjectState] = []
        for path in self.runs_root.glob("*/state.json"):
            try:
                states.append(ProjectState.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(states, key=lambda state: state.trace[-1].ts if state.trace else datetime.min.replace(tzinfo=timezone.utc), reverse=True)


class TraceRecorder:
    """Append typed trace events; M7 exposes these through an SSE stream."""

    @staticmethod
    def add(
        state: ProjectState,
        *,
        agent: str,
        kind: str,
        summary: str,
        data: dict[str, object] | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            ts=datetime.now(timezone.utc),
            stage=state.stage,
            agent=agent,
            kind=kind,  # type: ignore[arg-type] - caller uses the state contract literals.
            summary=summary,
            data=data or {},
        )
        state.trace.append(event)
        return event
