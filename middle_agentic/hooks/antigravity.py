"""Antigravity hooks adapter (https://antigravity.google/docs/hooks/).

Antigravity payloads carry no event name, so the hook script passes the event as the URL path
(`/v1/hooks/antigravity/<Event>`). Payload fields are camelCase and common to every event.

    SessionStart   -> recent project memory as a systemMessage in injectSteps
    PreInvocation  -> on the first invocation of a turn: record lastUserInput, and inject memory
                      relevant to it as an ephemeralMessage. Later invocations of the same turn
                      (tool loop) are skipped so the prompt is recorded once.
    anything else  -> {} (PostInvocation has no assistant text in its payload, so answers are not recorded)
"""
from __future__ import annotations

from typing import Any

from middle_agentic.hooks.common import HookDeps, project_session_id
from middle_agentic.memory.graph import format_context

# Keep injected text well inside what a single step can carry.
MAX_CONTEXT_CHARS = 9_000


def _session_for(payload: dict[str, Any]) -> str:
    paths = payload.get("workspacePaths") or []
    return project_session_id(paths[0] if paths else None)


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "antigravity",
        "conversation_id": payload.get("conversationId"),
        "model": payload.get("modelName"),
    }


def _inject(text: str) -> dict[str, Any]:
    return {"injectSteps": [{"ephemeralMessage": text[:MAX_CONTEXT_CHARS]}]}


async def handle(event: str, payload: dict[str, Any], deps: HookDeps) -> dict[str, Any]:
    if not deps.enabled:
        return {}

    session_id = _session_for(payload)

    if event == "SessionStart":
        rows = await deps.memory.fetch_context(session_id, query="", related_limit=0)
        context = format_context(rows, deps.snippet_chars)
        if not context:
            return {}
        return {"injectSteps": [{"systemMessage": {"systemMessage": context[:MAX_CONTEXT_CHARS]}}]}

    if event == "PreInvocation":
        if int(payload.get("invocationNum") or 0) != 0:
            return {}
        prompt = str(payload.get("lastUserInput") or "").strip()
        if not prompt:
            return {}
        # Read before writing so the prompt does not show up as its own context.
        rows = await deps.memory.fetch_context(session_id, prompt)
        await deps.memory.add_node(session_id, "user", prompt, _metadata(payload))
        context = format_context(rows, deps.snippet_chars)
        return _inject(context) if context else {}

    return {}
