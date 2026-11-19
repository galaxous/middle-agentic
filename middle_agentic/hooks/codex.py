"""Codex CLI hooks adapter (learn.chatgpt.com/docs/hooks; schemas in openai/codex codex-rs/hooks/schema).

Payloads are snake_case and carry hook_event_name, like Claude Code's.

    SessionStart      -> recent project memory as hookSpecificOutput.additionalContext
    UserPromptSubmit  -> memory relevant to the prompt as additionalContext, then record the prompt
    Stop              -> record last_assistant_message
    anything else     -> {}

Codex runs non-managed hooks only after they are trusted (`/hooks` in the CLI), so the first
install needs one manual review. The installer sets additionalContextLimit to match MAX_CONTEXT_CHARS.
"""
from __future__ import annotations

from typing import Any

from middle_agentic.hooks.common import HookDeps, project_session_id
from middle_agentic.memory.graph import format_context

# Installed hooks declare additionalContextLimit=9000; stay below it.
MAX_CONTEXT_CHARS = 8_000


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {"source": "codex", "codex_session_id": payload.get("session_id"), "turn_id": payload.get("turn_id")}


def _context_output(event: str, context: str | None) -> dict[str, Any]:
    if not context:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context[:MAX_CONTEXT_CHARS]}}


async def handle(event: str, payload: dict[str, Any], deps: HookDeps) -> dict[str, Any]:
    if not deps.enabled:
        return {}

    session_id = project_session_id(payload.get("cwd"))

    if event == "SessionStart":
        rows = await deps.memory.fetch_context(session_id, query="", related_limit=0)
        return _context_output(event, format_context(rows, deps.snippet_chars))

    if event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return {}
        # Read before writing so the prompt does not show up as its own context.
        rows = await deps.memory.fetch_context(session_id, prompt)
        await deps.memory.add_node(session_id, "user", prompt, _metadata(payload))
        return _context_output(event, format_context(rows, deps.snippet_chars))

    if event == "Stop":
        text = str(payload.get("last_assistant_message") or "").strip()
        if text:
            await deps.memory.add_node(session_id, "assistant", text, _metadata(payload))
        return {}

    return {}
