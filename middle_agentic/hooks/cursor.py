"""Cursor agent hooks adapter (https://cursor.com/docs/agent/hooks).

Cursor can only inject context at ``sessionStart`` (``additional_context``); per-prompt context
in Cursor comes from the MCP ``memory_search`` tool instead.

    sessionStart        -> recent project memory as additional_context
    beforeSubmitPrompt  -> record user prompt, always {"continue": true}
    afterAgentResponse  -> record assistant answer
    anything else       -> acknowledged with {}
"""
from __future__ import annotations

from typing import Any

from middle_agentic.hooks.common import HookDeps, project_session_id
from middle_agentic.memory.graph import format_context


def _session_for(payload: dict[str, Any]) -> str:
    roots = payload.get("workspace_roots") or []
    return project_session_id(roots[0] if roots else None)


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "cursor",
        "conversation_id": payload.get("conversation_id"),
        "generation_id": payload.get("generation_id"),
        "model": payload.get("model"),
    }


async def handle(event: str, payload: dict[str, Any], deps: HookDeps) -> dict[str, Any]:
    neutral: dict[str, Any] = {"continue": True} if event == "beforeSubmitPrompt" else {}
    if not deps.enabled:
        return neutral

    session_id = _session_for(payload)

    if event == "sessionStart":
        rows = await deps.memory.fetch_context(session_id, query="", related_limit=0)
        context = format_context(rows, deps.snippet_chars)
        return {"additional_context": context} if context else {}

    if event == "beforeSubmitPrompt":
        prompt = str(payload.get("prompt") or "").strip()
        if prompt:
            await deps.memory.add_node(session_id, "user", prompt, _metadata(payload))
        return neutral

    if event == "afterAgentResponse":
        text = str(payload.get("text") or "").strip()
        if text:
            await deps.memory.add_node(session_id, "assistant", text, _metadata(payload))
        return {}

    return neutral
