"""Agent-to-agent (A2A) communication between the CrewAI side and the ADK planner.

The ADK planning agent is served as an A2A server (``a2a_planner.py``, built
with ADK's ``to_a2a``).  Two clients talk to it over the A2A protocol:

* ``A2APlannerExecutor`` - the orchestrator's planning role.  Plans and
  replans are requested from the remote ADK agent as A2A ``message/send``
  calls, and the reply is validated into ReForge's ``Plan`` model.
* ``consult_planner_tool`` - a CrewAI tool given to the CrewAI repair agent,
  so a CrewAI agent can ask the ADK planner whether a fix fits the approved
  plan, agent to agent.

The client below implements the protocol directly (agent-card discovery plus
JSON-RPC ``message/send``) so it does not depend on fast-moving SDK client APIs.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ValidationError

from ..core import tracing
from ..core.input_guard import harden_prompt
from .executor import TokenUsage
from .monitoring import AgentMonitor

ModelT = TypeVar("ModelT", bound=BaseModel)
CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")


class A2AError(RuntimeError):
    """Raised when the remote agent is unreachable or replies with an error."""


class A2AUnavailable(A2AError):
    """The remote agent could not be reached at all (connection refused, DNS, timeout)."""


@dataclass(slots=True)
class A2AClient:
    """Minimal A2A protocol client: discover the agent card, send a text message, read the reply."""

    base_url: str
    timeout_s: float = 300.0
    opener: Callable[..., Any] = urlopen
    _card: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def agent_card(self) -> dict[str, Any]:
        if self._card is None:
            last: Exception | None = None
            for path in CARD_PATHS:
                try:
                    self._card = self._get(self.base_url.rstrip("/") + path)
                    break
                except A2AError as exc:
                    last = exc
            if self._card is None:
                error = A2AUnavailable if isinstance(last, A2AUnavailable) else A2AError
                raise error(f"no A2A agent card at {self.base_url}: {last}")
        return self._card

    def send_text(self, text: str, *, context_id: str | None = None) -> str:
        """``message/send`` one user text message; return the agent's final text."""
        card = self.agent_card()
        endpoint = str(card.get("url") or self.base_url)
        message: dict[str, Any] = {
            "kind": "message",
            "role": "user",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        body = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {"message": message, "configuration": {"blocking": True, "acceptedOutputModes": ["text/plain", "application/json"]}},
        }
        with tracing.span("a2a.message_send", a2a__agent=str(card.get("name", "")), a2a__endpoint=endpoint, a2a__characters=len(text)):
            reply = self._post(endpoint, body)
        if "error" in reply:
            error = reply["error"] or {}
            raise A2AError(f"A2A error {error.get('code')}: {error.get('message')}")
        return extract_reply_text(reply.get("result"))

    def _get(self, url: str) -> dict[str, Any]:
        return self._request(Request(url, headers={"Accept": "application/json"}))

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST"))

    def _request(self, request: Request) -> dict[str, Any]:
        try:
            with self.opener(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, json.JSONDecodeError) as exc:  # HTTPError first: it subclasses URLError
            raise A2AError(f"A2A request to {request.full_url} failed: {exc}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            raise A2AUnavailable(f"A2A agent at {request.full_url} is unreachable: {exc}") from exc
        if not isinstance(payload, dict):
            raise A2AError("A2A reply was not a JSON object")
        return payload


def extract_reply_text(result: Any) -> str:
    """Final text from an A2A ``Task`` (artifacts, then status message, then history) or ``Message``."""
    if not isinstance(result, dict):
        raise A2AError("A2A reply had no result")
    if result.get("kind") == "message":
        return _parts_text(result.get("parts"))
    state = (result.get("status") or {}).get("state")
    if state in {"failed", "rejected", "canceled", "cancelled"}:
        raise A2AError(f"remote task {state}: {_parts_text(((result.get('status') or {}).get('message') or {}).get('parts'))}")
    text = "".join(_parts_text(artifact.get("parts")) for artifact in result.get("artifacts") or [] if isinstance(artifact, dict))
    if not text.strip():
        text = _parts_text(((result.get("status") or {}).get("message") or {}).get("parts"))
    if not text.strip():
        agent_messages = [item for item in result.get("history") or [] if isinstance(item, dict) and item.get("role") == "agent"]
        text = _parts_text(agent_messages[-1].get("parts")) if agent_messages else ""
    if not text.strip():
        raise A2AError("remote agent returned no text")
    return text


def _parts_text(parts: Any) -> str:
    return "".join(str(part.get("text", "")) for part in parts or [] if isinstance(part, dict) and part.get("kind", "text") == "text")


@dataclass(slots=True)
class A2APlannerExecutor:
    """The planning role, executed by the remote ADK planner over A2A."""

    framework: ClassVar[str] = "adk-a2a"

    client: A2AClient
    monitor: AgentMonitor | None = None
    # In-process planner used when the A2A service cannot be reached, so one stopped container does not fail runs.
    fallback: Any = None
    usage: TokenUsage = field(default_factory=TokenUsage)  # A2A replies carry no token counts
    _context_id: str | None = field(default=None, init=False)

    def set_run_context(self, *, run_id: str, workspace: Any) -> None:
        # One A2A conversation context per run keeps the planner's plan and replans together.
        self._context_id = f"reforge-{run_id}"

    def run(
        self,
        *,
        role: str,
        goal: str,
        backstory: str,
        prompt: str,
        expected_output: str,
        output_model: type[ModelT],
        validator: Callable[[ModelT], None] | None = None,
    ) -> ModelT:
        hardened, findings = harden_prompt(prompt)
        if self.monitor is not None:
            self.monitor.record("planning", "tool_call", f"Sent planning request to the ADK planner over A2A ({self.client.base_url})")
            for finding in findings:
                self.monitor.record("planning", "decision", f"Possible prompt injection in quoted content ({finding.rule}); treated as data", excerpt=finding.excerpt)
        message = f"{hardened}\n\nRespond with {expected_output} Return only the JSON object."
        try:
            text = self.client.send_text(message, context_id=self._context_id)
        except A2AUnavailable as exc:
            if self.fallback is None:
                raise
            if self.monitor is not None:
                self.monitor.record("planning", "decision", f"A2A planner unreachable; planning in-process instead. {str(exc)[:200]}")
            return self.fallback.run(
                role=role, goal=goal, backstory=backstory, prompt=prompt,
                expected_output=expected_output, output_model=output_model, validator=validator,
            )
        try:
            value = output_model.model_validate(json.loads(_strip_fence(text)))
            if validator is not None:
                validator(value)
        except (ValidationError, ValueError, TypeError) as exc:
            if self.monitor is not None:
                self.monitor.record("planning", "error", f"A2A planner reply failed validation: {str(exc)[:200]}")
            raise A2AError(f"A2A planner did not return a valid {output_model.__name__}: {exc}") from exc
        if self.monitor is not None:
            self.monitor.record("planning", "result", f"ADK planner replied over A2A with a valid {output_model.__name__}")
        return value


def consult_planner_tool(client: A2AClient, monitor: AgentMonitor | None = None) -> Any:
    """A CrewAI tool that lets a CrewAI agent ask the ADK planner a question over A2A."""
    from crewai.tools import BaseTool
    from pydantic import Field as PydanticField

    class ConsultPlannerInput(BaseModel):
        question: str = PydanticField(description="What to ask the ADK planning agent, e.g. whether a proposed fix fits the approved plan.")

    class ConsultPlannerTool(BaseTool):
        name: str = "consult_reforge_planner"
        description: str = (
            "Ask ReForge's ADK planning agent (over the A2A protocol) whether a fix fits the approved "
            "modernization plan or which plan step a failure belongs to. Returns the planner's answer as text."
        )
        args_schema: type[BaseModel] = ConsultPlannerInput

        def _run(self, question: str) -> str:
            if monitor is not None:
                monitor.record("repair", "tool_call", "CrewAI repair agent consulted the ADK planner over A2A", question=question[:300])
            hardened, _ = harden_prompt(question)
            return client.send_text(f"{hardened}\n\nAnswer in at most five sentences of plain text.")

    return ConsultPlannerTool()


def _strip_fence(raw: str) -> str:
    value = raw.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else ""
        if value.rstrip().endswith("```"):
            value = value.rstrip()[:-3]
    return value.strip()
