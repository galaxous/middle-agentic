"""Scopes: a key can only do what its scopes allow, on REST and on MCP tool calls."""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.server.app import create_app

MCP_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _client_with(settings, fake_llm, scopes: str) -> tuple[TestClient, dict[str, str]]:
    store = ApiKeyStore(settings.memory.resolved_db_path())
    key = store.create(f"k-{abs(hash(scopes))}-{len(scopes)}", scopes=scopes)
    return TestClient(create_app(settings)), {"Authorization": f"Bearer {key}"}


def test_read_only_key_can_search_but_not_write_chat_or_delete(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "memory:read")
    with client:
        assert client.post("/v1/memory/context", json={"query": "x"}, headers=headers).status_code == 200
        assert client.get("/v1/memory/sessions", headers=headers).status_code == 200
        assert client.post("/v1/memory/events", json={"content": "x"}, headers=headers).status_code == 403
        assert client.post("/v1/memory/forget", json={"ids": ["a"]}, headers=headers).status_code == 403
        chat = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat/completions", json=chat, headers=headers).status_code == 403


def test_write_only_key_can_record_but_not_read(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "memory:write")
    with client:
        assert client.post("/v1/memory/events", json={"content": "decided x"}, headers=headers).status_code == 201
        assert client.post("/v1/memory/context", json={"query": "x"}, headers=headers).status_code == 403


def test_models_key_runs_chat_only(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "models")
    with client:
        chat = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat/completions", json=chat, headers=headers).status_code == 200
        assert client.post("/v1/memory/context", json={}, headers=headers).status_code == 403


def test_hooks_need_read_and_write(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "memory:read")
    with client:
        payload = {"hook_event_name": "Stop", "session_id": "s", "cwd": "/tmp/p", "last_assistant_message": "x"}
        assert client.post("/v1/hooks/claude/Stop", json=payload, headers=headers).status_code == 403


def test_delete_scope_is_needed_for_forget(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "memory:delete")
    with client:
        assert client.post("/v1/memory/forget", json={"session_id": "s", "ids": ["none"]}, headers=headers).status_code == 200


def test_mcp_tool_calls_are_checked_by_scope(settings, fake_llm) -> None:
    client, headers = _client_with(settings, fake_llm, "memory:read")
    with client:
        def call(tool: str):
            body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": {"prompt": "x", "content": "x"}}}
            return client.post("/mcp/", json=body, headers={**MCP_ACCEPT, **headers})

        ask = call("ask")
        assert ask.status_code == 403 and "models" in ask.text
        record = call("memory_record")
        assert record.status_code == 403 and "memory:write" in record.text
        # memory_search is allowed for this key: the guard lets it through to the SDK
        assert call("memory_search").status_code != 403


def test_key_without_scopes_is_unknown(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:  # a table from before scopes existed
        connection.execute("CREATE TABLE gateway_api_keys (name TEXT PRIMARY KEY, key_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, last_used_at REAL, revoked INTEGER NOT NULL DEFAULT 0)")
        connection.execute("INSERT INTO gateway_api_keys (name, key_hash, created_at) VALUES ('old', ?, 0)", (
            __import__("hashlib").sha256(b"mag_legacy").hexdigest(),))
    store = ApiKeyStore(path)  # migrates the column
    assert store.scopes_for("mag_legacy") is None  # fail closed
    assert store.verify("mag_legacy") is False


def test_unknown_scope_and_empty_scope_are_refused(tmp_path) -> None:
    import pytest
    from middle_agentic.server.api_keys import ApiKeyError

    store = ApiKeyStore(tmp_path / "k.db")
    with pytest.raises(ApiKeyError, match="unknown scope"):
        store.create("a", scopes="memory:read,admin")
    with pytest.raises(ApiKeyError, match="at least one"):
        store.create("b", scopes=" , ")


def test_set_scopes_changes_access_immediately(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    key = store.create("laptop", scopes="memory:read")
    assert store.scopes_for(key) == frozenset({"memory:read"})
    assert store.set_scopes("laptop", "memory:read,models")
    assert store.scopes_for(key) == frozenset({"memory:read", "models"})
