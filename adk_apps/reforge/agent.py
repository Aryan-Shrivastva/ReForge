"""ADK web UI entry point for browsing ReForge run sessions.

``adk web adk_apps`` lists one app per folder; this folder is named ``reforge``
to match ``APP_NAME`` in ``reforge/backend/app/agents/adk_adapter.py``, so the
dev UI shows every ReForge run session (its events and state deltas) from the
shared session database.

The agent defined here is a read-only *run inspector*: chatting with it in the
dev UI answers questions about the selected run from its session state.  It
cannot change a run.  ReForge's own analysis and planning agents are built per
call by the workflow and are not served from here.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from reforge.backend.app.agents.adk_adapter import ADKExecutor, _load_adk
from reforge.backend.app.core.config import Settings

_settings = Settings.from_env()
_model = ADKExecutor(
    provider=_settings.llm_provider,
    model=_settings.llm_model or "gemini-2.5-flash",
    temperature=_settings.llm_temperature,
)._model(_load_adk())

root_agent = LlmAgent(
    name="reforge_run_inspector",
    model=_model,
    description="Answers questions about a ReForge modernization run from its session state.",
    instruction=(
        "You are the ReForge run inspector. Answer questions about this modernization run using only the session "
        "state below. You cannot modify the repository or the run; say so if asked.\n"
        "- Run: {run_id?}\n"
        "- Repository: {repository?}\n"
        "- Objective: {objective?}\n"
        "- Stage: {stage?} (final status: {final_status?})\n"
        "- Languages: {languages?}\n"
        "- Analysis: {analysis_summary?}\n"
        "- Plan: {plan_summary?}\n"
        "- Repairs: {repair_history?}\n"
        "- Agent history: {agent_history?}\n"
        "- Tokens used: {tokens_used?}"
    ),
    include_contents="none",
)
