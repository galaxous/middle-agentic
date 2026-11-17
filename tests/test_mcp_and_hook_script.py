"""End-to-end over real processes: the MCP server on stdio and the stdlib hook bridge script."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from middle_agentic.config import PROJECT_ROOT

from conftest import API_KEY

HOOK_SCRIPT = PROJECT_ROOT / "integrations" / "hooks" / "magent_hook.py"


def _env(tmp_path, url: str) -> dict[str, str]:
    config_path = tmp_path / "client.yaml"
    config_path.write_text("discovery: {tailscale: false}\nnode: {roles: [client]}\n", encoding="utf-8")
    return {
        **os.environ,
        "MAGENT_URL": url,
        "MAGENT_API_KEY": API_KEY,
        "MAGENT_SESSION": "mcp-test",
        "MAGENT_CONFIG": str(config_path),
    }


async def test_mcp_tools_round_trip(live_server: str, tmp_path) -> None:
    params = StdioServerParameters(command=sys.executable, args=["-m", "middle_agentic", "mcp"], env=_env(tmp_path, live_server))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert tools == {"ask", "memory_search", "memory_record", "list_nodes"}

        recorded = await session.call_tool("memory_record", {"content": "Billing service owns invoices", "role": "fact"})
        assert "session mcp-test" in recorded.content[0].text

        found = await session.call_tool("memory_search", {"query": "who owns invoices"})
        assert "Billing service owns invoices" in found.content[0].text

        answer = await session.call_tool("ask", {"prompt": "hi", "model": "paid"})
        assert "answer from openai/m-paid" in answer.content[0].text
        assert "tier=paid" in answer.content[0].text


def run_hook(tool: str, payload: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), tool], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20
    )


def test_hook_script_forwards_to_gateway(live_server: str, tmp_path) -> None:
    env = _env(tmp_path, live_server)
    payload = {"hook_event_name": "UserPromptSubmit", "prompt": "deploy steps?", "cwd": "/tmp/project", "session_id": "x"}
    result = run_hook("claude", payload, env)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}

    payload["prompt"] = "deploy steps again?"
    result = run_hook("claude", payload, env)
    assert "deploy steps?" in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_hook_script_fails_open(tmp_path) -> None:
    env = _env(tmp_path, "http://127.0.0.1:9")
    result = run_hook("cursor", {"hook_event_name": "beforeSubmitPrompt", "prompt": "x"}, env)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"continue": True}
    assert "gateway unavailable" in result.stderr

    result = run_hook("claude", {"hook_event_name": "Stop"}, {**env, "MAGENT_API_KEY": "wrong"})
    assert result.returncode == 0
    assert json.loads(result.stdout) == {}
