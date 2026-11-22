"""FastAPI application factory for the middle-agent gateway.

Run:
    magent serve
    uvicorn middle_agentic.server.app:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import ipaddress
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from middle_agentic import __version__
from middle_agentic.config import Settings, load_settings
from middle_agentic.memory.graph import GraphMemory
from middle_agentic.providers.chain import ModelRouter
from middle_agentic.providers.registry import resolve_chain
from middle_agentic.server import routes_chat, routes_hooks, routes_memory
from middle_agentic.server.mcp_http import mcp_lifespan, mount_mcp
from middle_agentic.server.state import GatewayState, get_state

logger = logging.getLogger("middle_agentic.server")


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings = settings or load_settings()
    memory = GraphMemory(
        settings.memory.resolved_db_path(),
        recent_limit=settings.memory.recent_limit,
        related_limit=settings.memory.related_limit,
        important_limit=settings.memory.important_limit,
    )
    router = ModelRouter(
        resolve_chain(settings.chain),
        request_timeout=settings.gateway.request_timeout,
        probe_timeout=settings.discovery.probe_timeout,
    )
    state = GatewayState(settings=settings, memory=memory, router=router)

    # Created before the app so the lifespan can start the MCP session manager.
    mcp_holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.memory.enabled:
            await memory.initialize()
            logger.info("graph memory ready at %s", memory.db_path)
        for index, tier in enumerate(await router.describe(), start=1):
            logger.info("tier %d %-16s [%s] %s/%s -> %s", index, tier["name"], tier["tier"], tier["runtime"], tier["model"], tier["reason"])
        if not settings.gateway.api_key and not _is_loopback(settings.gateway.host):
            logger.warning("GATEWAY_API_KEY is empty while listening on %s: anyone on the network can use your keys", settings.gateway.host)
        async with mcp_lifespan(mcp_holder["app"]):
            yield

    app = FastAPI(title="Middle-Agent Gateway", version=__version__, lifespan=lifespan)
    app.state.gateway = state
    app.include_router(routes_chat.router)
    app.include_router(routes_memory.router)
    app.include_router(routes_hooks.router)
    mcp_holder["app"] = mount_mcp(app, state)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        current = get_state(request)
        payload: dict[str, Any] = {
            "status": "ok",
            "service": "middle-agentic",
            "version": __version__,
            "node": {"name": current.settings.node.name, "roles": current.settings.node.roles},
            "auth_required": bool(current.settings.gateway.api_key),
            "memory_enabled": current.memory_enabled,
            "chain": await current.router.describe(),
        }
        if current.memory_enabled:
            try:
                payload["memory"] = await current.memory.stats()
            except sqlite3.Error as exc:
                payload["status"] = "degraded"
                payload["memory_error"] = str(exc)
        return payload

    return app
