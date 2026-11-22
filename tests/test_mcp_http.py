"""The gateway's own MCP endpoint at /mcp: auth, no redirect, and the tools end to end."""
from __future__ import annotations

import httpx2
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from middle_agentic.server.app import create_app

from conftest import API_KEY, AUTH

MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


def test_mcp_without_key_is_401_and_not_redirected(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        for path in ("/mcp", "/mcp/"):
            response = client.post(path, json=INIT, headers={"Accept": "application/json, text/event-stream"}, follow_redirects=False)
            assert response.status_code == 401, path
        wrong = client.post("/mcp/", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}, follow_redirects=False)
        assert wrong.status_code == 401


def test_mcp_with_key_is_not_redirected(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.post("/mcp", json=INIT, headers=MCP_HEADERS, follow_redirects=False)
        assert response.status_code == 200  # no 307 to /mcp/


async def test_mcp_tools_round_trip_over_http(live_server: str) -> None:
    url = f"{live_server}/mcp"
    async with httpx2.AsyncClient(headers={**AUTH, "Authorization": f"Bearer {API_KEY}"}) as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {"ask", "memory_search", "memory_record", "list_nodes"}

                recorded = await session.call_tool(
                    "memory_record",
                    {"content": "Checkout uses Stripe webhooks", "role": "decision", "project_dir": "/tmp/mcp-http-shop"},
                )
                assert "Recorded decision node" in recorded.content[0].text

                found = await session.call_tool(
                    "memory_search", {"query": "Stripe webhooks", "project_dir": "/tmp/mcp-http-shop"}
                )
                assert "Checkout uses Stripe webhooks" in found.content[0].text

                other = await session.call_tool("memory_search", {"query": "Stripe webhooks", "project_dir": "/tmp/elsewhere"})
                assert "No matching memory" in other.content[0].text  # project scope holds

                bad_role = await session.call_tool("memory_record", {"content": "x", "role": "user"})
                assert "role must be one of" in bad_role.content[0].text


async def test_mcp_ask_goes_through_the_model_chain(live_server: str, fake_llm) -> None:
    async with httpx2.AsyncClient(headers=AUTH) as http:
        async with streamable_http_client(f"{live_server}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("ask", {"prompt": "hello", "model": "auto"})
                assert "answer from" in result.content[0].text
                assert "[node=" in result.content[0].text
