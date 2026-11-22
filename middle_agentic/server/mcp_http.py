"""MCP endpoint hosted by the gateway itself (streamable HTTP at /mcp).

Same tools as `magent mcp` (stdio), but they run inside the gateway process and use its memory and
model router directly, so a client needs only the URL and the bearer key, not a local Python install.

    ask, memory_search, memory_record, list_nodes
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from starlette.responses import JSONResponse

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from middle_agentic.client.discovery import discover_nodes
from middle_agentic.hooks.common import project_session_id
from middle_agentic.memory.graph import format_context, normalise_session_id
from middle_agentic.providers.chain import AllTiersFailedError, UnknownModelError
from middle_agentic.server.routes_chat import message_text, post_hook, pre_hook
from middle_agentic.server.state import GatewayState

try:
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError:  # pragma: no cover - mcp 1.x layout
    TransportSecuritySettings = None  # type: ignore[assignment,misc]

MCP_PATH = "/mcp"
MAX_CONTENT_CHARS = 100_000
ROLES = ("decision", "fact", "note")
INSTRUCTIONS = (
    "middle-agentic is the memory and model gateway of the user's own machines. "
    "Use memory_search before answering questions about earlier decisions or project history, "
    "memory_record to persist decisions and facts worth remembering, ask to delegate a prompt to the "
    "gateway's model chain, and list_nodes to see which nodes are online. Pass project_dir (absolute "
    "workspace root) so the right project's memory is used."
)


def _session(session_id: str | None, project_dir: str | None) -> str:
    if session_id:
        return normalise_session_id(session_id)
    if project_dir:
        return project_session_id(project_dir)
    return "default"


def build_gateway_mcp(state: GatewayState) -> _Server:
    server = _Server("middle-agentic", instructions=INSTRUCTIONS)

    @server.tool(description="Send a prompt through the gateway's model chain and return the answer.")
    async def ask(
        prompt: str,
        model: str = "auto",
        system: str | None = None,
        use_memory: bool = True,
        project_dir: str | None = None,
    ) -> str:
        """model: 'auto' (fallback chain), a tier name like 'local' or 'luna', or a label: free/local/paid."""
        session = _session(None, project_dir)
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        if use_memory and state.memory_enabled:
            messages = await pre_hook(state, session, messages, prompt)
        try:
            response, tier, _attempts = await state.router.complete(messages, {}, model)
        except UnknownModelError as exc:
            return f"Unknown model: {exc}"
        except AllTiersFailedError as exc:
            return f"All model tiers failed ({len(exc.attempts)} attempt(s)); check `magent health`."
        payload = jsonable_encoder(response.model_dump())
        choices = payload.get("choices") or [{}]
        text = message_text((choices[0].get("message") or {}).get("content"))
        if use_memory and state.memory_enabled and text:
            await post_hook(state, session, prompt, text, {"tier": tier.name, "model": tier.model})
        return f"{text}\n\n[node={state.settings.node.name} tier={tier.name} model={tier.model}]"

    @server.tool(
        description=(
            "Search project memory (decisions, facts, notes and earlier conversation). Decisions and facts "
            "rank first. Pass project_dir (absolute workspace root) so the right project's memory is used."
        )
    )
    async def memory_search(
        query: str, project_dir: str | None = None, session_id: str | None = None, limit: int = 8
    ) -> str:
        if not state.memory_enabled:
            return "Memory is disabled on this gateway."
        rows = await state.memory.fetch_context(
            _session(session_id, project_dir), query, recent_limit=0, related_limit=limit
        )
        return format_context(rows, state.settings.memory.snippet_chars) or "No matching memory."

    @server.tool(
        description=(
            "Store one self-contained sentence in project memory for later sessions and other agents. "
            "role: 'decision' (choice + reason), 'fact' (stable project fact) or 'note'. "
            "Pass project_dir (absolute workspace root)."
        )
    )
    async def memory_record(
        content: str, role: str = "note", project_dir: str | None = None, session_id: str | None = None
    ) -> str:
        if not state.memory_enabled:
            return "Memory is disabled on this gateway."
        if role not in ROLES:
            return f"role must be one of: {', '.join(ROLES)}"
        if not content.strip() or len(content) > MAX_CONTENT_CHARS:
            return f"content must be 1-{MAX_CONTENT_CHARS} characters"
        session = _session(session_id, project_dir)
        node_id = await state.memory.add_node(session, role, content, {"source": "mcp-http"})
        return f"Recorded {role} node {node_id} in session {session}."

    @server.tool(description="List middle-agentic nodes (local, configured peers, Tailscale) and whether they are online.")
    async def list_nodes() -> str:
        nodes = await asyncio.to_thread(discover_nodes, state.settings)
        return json.dumps([node.as_dict() for node in nodes], indent=2)

    return server


def build_mcp_app(state: GatewayState) -> Any:
    """Starlette app serving the MCP protocol at its root; mounted under /mcp by create_app.

    The MCP SDK's DNS-rebinding check only accepts localhost Host headers, which would reject the
    gateway's Tailscale name or IP. When a gateway API key is set, the bearer key is the gate, so the
    check is turned off. Without a key the default (localhost only) stays on, and the gateway already
    warns that it is open to the network.
    """
    options: dict[str, Any] = {"streamable_http_path": "/"}
    if state.settings.gateway.api_key and TransportSecuritySettings is not None:
        options["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return build_gateway_mcp(state).streamable_http_app(**options)


@contextlib.asynccontextmanager
async def mcp_lifespan(mcp_app: Any) -> AsyncIterator[None]:
    """The mounted sub-app does not run its own lifespan; its session manager must be started here."""
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


class McpGuard:
    """Pure ASGI middleware in front of the whole gateway.

    - `/mcp` is rewritten to `/mcp/` so MCP clients do not get a 307 on their POST.
    - Requests under `/mcp/` need `Authorization: Bearer <GATEWAY_API_KEY>` when a key is configured,
      the same check the REST endpoints use.
    """

    def __init__(self, app: Any, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == MCP_PATH:
            scope = {**scope, "path": MCP_PATH + "/", "raw_path": (MCP_PATH + "/").encode()}
        if scope["type"] == "http" and scope["path"].startswith(MCP_PATH + "/") and self.api_key:
            headers = dict(scope.get("headers") or [])
            scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                token.strip().encode(), self.api_key.encode()
            ):
                response = JSONResponse(
                    {"detail": "Invalid or missing API key"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def mount_mcp(app: FastAPI, state: GatewayState) -> Any:
    """Mounts /mcp and installs the guard. Returns the MCP sub-app so the caller can run its lifespan."""
    mcp_app = build_mcp_app(state)
    app.mount(MCP_PATH, mcp_app)
    app.add_middleware(McpGuard, api_key=state.settings.gateway.api_key)
    return mcp_app
