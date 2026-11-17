"""MCP server (stdio) exposing middle-agentic nodes as tools for Cursor, Claude Code, etc.

Tools: ask, memory_search, memory_record, list_nodes.
Memory is scoped to the current project by default (same scope as the Cursor/Claude hooks), so
whatever the hooks recorded is searchable here.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

from middle_agentic.client.discovery import discover_nodes
from middle_agentic.client.sdk import GatewayError, MiddleAgentClient
from middle_agentic.config import Settings, load_settings
from middle_agentic.hooks.common import project_session_id

INSTRUCTIONS = (
    "middle-agentic connects you to LLM gateway nodes on the user's tailnet. "
    "Use memory_search before answering questions about earlier decisions or project history, "
    "memory_record to persist decisions and facts worth remembering, ask to delegate a prompt to a "
    "node (model='local' keeps it on the node's own model, 'auto' uses the fallback chain), "
    "and list_nodes to see which nodes are online."
)


def default_session() -> str:
    explicit = os.getenv("MAGENT_SESSION")
    if explicit:
        return explicit
    project_dir = os.getenv("CURSOR_PROJECT_DIR") or os.getenv("CLAUDE_PROJECT_DIR") or os.getcwd()
    return project_session_id(project_dir)


def build_server(settings: Settings | None = None) -> _Server:
    settings = settings or load_settings()
    session = default_session()
    server = _Server("middle-agentic", instructions=INSTRUCTIONS)

    def client_for(node: str | None) -> MiddleAgentClient:
        return MiddleAgentClient(node=node, session_id=session, settings=settings)

    async def run_safely(func) -> str:
        try:
            return await asyncio.to_thread(func)
        except GatewayError as exc:
            return f"Gateway error ({exc.status_code}): {exc.detail}"
        except httpx.HTTPError as exc:
            return f"Could not reach the node: {type(exc).__name__}: {exc}"

    @server.tool(description="Send a prompt to a middle-agentic node and return its answer.")
    async def ask(
        prompt: str,
        node: str | None = None,
        model: str = "auto",
        system: str | None = None,
        use_memory: bool = True,
    ) -> str:
        """model: 'auto' (fallback chain), a tier name like 'local' or 'luna', or a label: free/local/paid."""

        def call() -> str:
            with client_for(node) as client:
                result = client.ask(prompt, model=model, system=system, memory=use_memory)
            return f"{result.text}\n\n[node={result.node} tier={result.tier_name} model={result.model}]"

        return await run_safely(call)

    @server.tool(description="Search project memory (earlier prompts, answers, notes and decisions).")
    async def memory_search(query: str, node: str | None = None, session_id: str | None = None, limit: int = 8) -> str:
        def call() -> str:
            with client_for(node) as client:
                result = client.context(query, session_id=session_id, recent_limit=max(1, limit // 2), related_limit=limit)
            return result.get("context") or "No matching memory."

        return await run_safely(call)

    @server.tool(description="Store a note, decision or fact in project memory for later sessions and other agents.")
    async def memory_record(content: str, role: str = "note", node: str | None = None, session_id: str | None = None) -> str:
        def call() -> str:
            with client_for(node) as client:
                result = client.record_event(content, role=role, session_id=session_id, metadata={"source": "mcp"})
            return f"Recorded node {result['id']} in session {result['session_id']}."

        return await run_safely(call)

    @server.tool(description="List middle-agentic nodes (local, configured peers, Tailscale) and whether they are online.")
    async def list_nodes() -> str:
        nodes = await asyncio.to_thread(discover_nodes, settings)
        return json.dumps([node.as_dict() for node in nodes], indent=2)

    return server


def run() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    run()
