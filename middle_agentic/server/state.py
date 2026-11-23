from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from middle_agentic.config import Settings
from middle_agentic.hooks import HookDeps
from middle_agentic.memory.graph import GraphMemory
from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.providers.chain import ModelRouter
from middle_agentic.server.overrides import OverrideStore
from middle_agentic.server.usage import UsageLedger

logger = logging.getLogger("middle_agentic.server")


@dataclass
class GatewayState:
    settings: Settings
    memory: GraphMemory
    router: ModelRouter
    keys: ApiKeyStore
    usage: UsageLedger
    overrides: OverrideStore
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def budget_policy(self) -> tuple[float, str]:
        """Daily cap and on_exceed mode: runtime overrides first, then config.yaml."""
        runtime = self.overrides.budget()
        daily = float(runtime.get("daily_usd", self.settings.budget.daily_usd))
        on_exceed = str(runtime.get("on_exceed", self.settings.budget.on_exceed))
        return daily, on_exceed

    @property
    def memory_enabled(self) -> bool:
        return self.settings.memory.enabled

    def hook_deps(self) -> HookDeps:
        return HookDeps(memory=self.memory, enabled=self.memory_enabled, snippet_chars=self.settings.memory.snippet_chars)

    def spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Fire-and-forget with a strong reference, so the task is not garbage collected mid-run."""
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def get_state(request: Request) -> GatewayState:
    return request.app.state.gateway


def require_memory(request: Request) -> GatewayState:
    state = get_state(request)
    if not state.memory_enabled:
        raise HTTPException(status_code=503, detail="memory is disabled on this node (MEMORY_ENABLED=false)")
    return state
