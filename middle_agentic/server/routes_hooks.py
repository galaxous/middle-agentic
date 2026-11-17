"""Agent-tool hook adapters: POST /v1/hooks/{tool}/{event} with the tool's raw hook payload."""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from middle_agentic.hooks import HOOK_HANDLERS
from middle_agentic.server.auth import require_api_key
from middle_agentic.server.state import get_state

logger = logging.getLogger("middle_agentic.server.hooks")

router = APIRouter(prefix="/v1/hooks", dependencies=[Depends(require_api_key)])


@router.post("/{tool}/{event}")
async def run_hook(tool: str, event: str, request: Request, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    handler = HOOK_HANDLERS.get(tool)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown hook tool '{tool}', supported: {sorted(HOOK_HANDLERS)}")
    try:
        return await handler(event, payload, get_state(request).hook_deps())
    except sqlite3.Error:
        # Hooks sit on the agent's critical path: degrade to a neutral answer instead of failing.
        logger.exception("hook %s/%s failed on memory access", tool, event)
        return {"continue": True} if (tool, event) == ("cursor", "beforeSubmitPrompt") else {}
