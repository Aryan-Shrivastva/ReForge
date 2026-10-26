"""Framework callbacks: monitoring, guardrails, and validation for CrewAI and ADK agents.

Both frameworks expose callback hooks.  ReForge attaches the same
``AgentMonitor`` to all of them, so intermediate outputs, tool calls, guardrail
findings, and errors from either framework land in one place:

* CrewAI - ``Agent.step_callback`` (every reasoning step and tool use),
  ``Task.callback`` (the finished task output), and ``Task.guardrail``
  (validates the output before CrewAI accepts it; a failure makes CrewAI retry
  with the reason).
* Google ADK - ``before_model_callback`` (prompt screening),
  ``after_model_callback`` (response checks), ``before_tool_callback`` and
  ``after_tool_callback`` (tool-argument logging and tool-return type checks).

The workflow drains the monitor after every agent call into the run's trace,
which the dashboard streams live, and each event is also attached to the
current OpenTelemetry span for LangFuse.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from ..core import tracing
from ..core.input_guard import screen_text

EventKind = Literal["tool_call", "result", "decision", "error"]
MAX_TOOL_RESULT_CHARS = 60_000


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    agent: str
    kind: EventKind
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


class AgentMonitor:
    """Thread-safe collector of callback events, drained into the run trace."""

    def __init__(self) -> None:
        self._events: list[MonitorEvent] = []
        self._lock = threading.Lock()

    def record(self, agent: str, kind: EventKind, summary: str, **data: Any) -> None:
        event = MonitorEvent(agent=agent, kind=kind, summary=summary[:500], data=_json_safe(data))
        with self._lock:
            self._events.append(event)
        tracing.add_event(f"{agent}.{kind}", summary=event.summary, **{k: str(v)[:500] for k, v in event.data.items()})

    def drain(self) -> list[MonitorEvent]:
        with self._lock:
            events, self._events = self._events, []
        return events


def _json_safe(data: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(data, default=str))
    except (TypeError, ValueError):
        return {key: str(value) for key, value in data.items()}


def _preview(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


# ---------------------------------------------------------------- CrewAI


def crewai_step_callback(monitor: AgentMonitor | None, agent: str) -> Callable[[Any], None]:
    """``Agent.step_callback``: log each reasoning step or tool use (AgentAction / AgentFinish / ToolResult)."""

    def callback(step: Any) -> None:
        if monitor is None:
            return
        tool = getattr(step, "tool", None)
        if tool:
            monitor.record(agent, "tool_call", f"Called tool {tool}", tool=tool, input=_preview(getattr(step, "tool_input", "")), result=_preview(getattr(step, "result", "")))
            return
        text = getattr(step, "output", None) or getattr(step, "text", None) or getattr(step, "result", None) or step
        monitor.record(agent, "result", f"{type(step).__name__}: {_preview(text, 160)}")

    return callback


def crewai_task_callback(monitor: AgentMonitor | None, agent: str) -> Callable[[Any], None]:
    """``Task.callback``: record the finished task's raw output size and structured result type."""

    def callback(output: Any) -> None:
        if monitor is None:
            return
        raw = getattr(output, "raw", "") or ""
        structured = getattr(output, "pydantic", None)
        monitor.record(agent, "result", f"Task finished ({len(raw)} characters, {type(structured).__name__ if structured is not None else 'unstructured'})")

    return callback


def crewai_output_guardrail(monitor: AgentMonitor | None, agent: str, output_model: type[BaseModel]) -> Callable[[Any], tuple[bool, Any]]:
    """``Task.guardrail``: accept only output that validates against the contracted schema.

    Returning ``(False, reason)`` makes CrewAI retry the task with the reason,
    so a malformed answer is corrected inside the framework before ReForge's
    own validation sees it.
    """

    def guardrail(output: Any) -> tuple[bool, Any]:
        structured = getattr(output, "pydantic", None)
        try:
            if isinstance(structured, output_model):
                return True, output
            raw = getattr(output, "raw", None) or str(output)
            output_model.model_validate(json.loads(_strip_fence(raw)))
            return True, output
        except (ValidationError, ValueError, TypeError) as exc:
            reason = f"Output must be one {output_model.__name__} JSON object matching the schema: {_preview(exc, 400)}"
            if monitor is not None:
                monitor.record(agent, "decision", f"Output guardrail rejected the answer; CrewAI will retry. {_preview(exc, 200)}")
            return False, reason

    return guardrail


# ---------------------------------------------------------------- Google ADK


@dataclass(slots=True)
class AdkCallbacks:
    """The four ADK callback hooks, bound to one monitor and agent name."""

    monitor: AgentMonitor | None
    agent: str

    def before_model(self, callback_context: Any, llm_request: Any) -> Any:
        """Screen the outgoing request; findings in quoted repository text are flagged, not blocked."""
        text = _request_text(llm_request)
        findings = [finding for finding in screen_text(text) if finding.kind == "prompt_injection"]
        if self.monitor is not None:
            self.monitor.record(self.agent, "tool_call", f"Model request ({len(text)} characters)")
            for finding in findings:
                self.monitor.record(self.agent, "decision", f"Possible prompt injection in quoted content ({finding.rule}); treated as data", excerpt=finding.excerpt)
        return None  # proceed with the model call

    def after_model(self, callback_context: Any, llm_response: Any) -> Any:
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []
        text = "".join(getattr(part, "text", None) or "" for part in parts)
        error = getattr(llm_response, "error_message", None)
        if self.monitor is not None:
            if error:
                self.monitor.record(self.agent, "error", f"Model error: {_preview(error, 300)}")
            elif text:
                self.monitor.record(self.agent, "result", f"Model response ({len(text)} characters)")
        return None  # keep the response unchanged

    def before_tool(self, tool: Any, args: dict[str, Any], tool_context: Any) -> Any:
        if self.monitor is not None:
            self.monitor.record(self.agent, "tool_call", f"Called tool {getattr(tool, 'name', tool)}", args=_preview(args))
        return None

    def after_tool(self, tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any) -> Any:
        """Type-check a tool's return value before the model sees it."""
        name = getattr(tool, "name", str(tool))
        checked, problem = validate_tool_response(tool_response)
        if self.monitor is not None:
            if problem:
                self.monitor.record(self.agent, "error", f"Tool {name} returned an invalid result: {problem}")
            else:
                self.monitor.record(self.agent, "result", f"Tool {name} returned {len(json.dumps(checked, default=str))} characters")
        return checked if problem or checked is not tool_response else None

    def as_kwargs(self) -> dict[str, Callable[..., Any]]:
        return {
            "before_model_callback": self.before_model,
            "after_model_callback": self.after_model,
            "before_tool_callback": self.before_tool,
            "after_tool_callback": self.after_tool,
        }


def validate_tool_response(response: Any) -> tuple[dict[str, Any], str | None]:
    """Tools must return a JSON object of bounded size; anything else is replaced by an error object."""
    if hasattr(response, "model_dump"):
        response = response.model_dump(mode="json")
    if not isinstance(response, dict):
        return {"error": f"tool returned {type(response).__name__}, expected an object"}, f"expected an object, got {type(response).__name__}"
    try:
        encoded = json.dumps(response, default=str)
    except (TypeError, ValueError) as exc:
        return {"error": "tool returned a value that is not JSON"}, f"not JSON-serialisable: {exc}"
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        return {"error": f"tool result exceeded {MAX_TOOL_RESULT_CHARS} characters and was withheld"}, "result too large"
    return response, None


def _request_text(llm_request: Any) -> str:
    chunks: list[str] = []
    for content in getattr(llm_request, "contents", None) or []:
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "text", None):
                chunks.append(part.text)
    return "\n".join(chunks)


def _strip_fence(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()
