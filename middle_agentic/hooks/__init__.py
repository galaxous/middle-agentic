from collections.abc import Awaitable, Callable
from typing import Any

from middle_agentic.hooks import claude, cursor
from middle_agentic.hooks.common import HookDeps, project_session_id

HookHandler = Callable[[str, dict[str, Any], HookDeps], Awaitable[dict[str, Any]]]

HOOK_HANDLERS: dict[str, HookHandler] = {
    "cursor": cursor.handle,
    "claude": claude.handle,
}

__all__ = ["HOOK_HANDLERS", "HookDeps", "HookHandler", "project_session_id"]
