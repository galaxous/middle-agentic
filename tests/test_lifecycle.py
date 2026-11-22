"""Key expiry, quotas, request audit log, and soft-deleted memory."""
from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from middle_agentic.memory.graph import GraphMemory
from middle_agentic.server.api_keys import ApiKeyStore, Denied
from middle_agentic.server.app import create_app
from middle_agentic.server.audit import AuditLog

from conftest import AUTH

MCP_ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _store(settings) -> tuple[ApiKeyStore, AuditLog]:
    audit = AuditLog(settings.memory.resolved_db_path())
    return ApiKeyStore(settings.memory.resolved_db_path(), audit=audit), audit


# ---- expiry ----------------------------------------------------------------------

def test_new_keys_expire_in_90_days_by_default(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    store.create("a", scopes="memory:read")
    expires = store.list()[0]["expires_at"]
    assert 89 * 86400 < expires - time.time() < 91 * 86400


def test_zero_days_means_never_expires(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    key = store.create("forever", scopes="memory:read", expires_days=0)
    assert store.list()[0]["expires_at"] is None
    assert store.authorize(key).expires_at is None


def test_expired_key_is_refused_with_a_clear_message(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    key = store.create("short", scopes="memory:read", expires_days=0.000001)  # about 0.09 s
    time.sleep(0.15)
    with pytest.raises(Denied) as info:
        store.authorize(key)
    assert info.value.status_code == 401 and "expired" in info.value.detail
    assert store.verify(key) is False


def test_renew_brings_an_expired_key_back(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    key = store.create("r", scopes="memory:read", expires_days=0.000001)
    time.sleep(0.15)
    assert store.renew("r", 30)
    assert store.authorize(key).name == "r"


# ---- quotas ----------------------------------------------------------------------

def test_per_minute_limit_returns_429(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    key = store.create("burst", scopes="memory:read", per_minute=2)
    store.authorize(key)
    store.authorize(key)
    with pytest.raises(Denied) as info:
        store.authorize(key)
    assert info.value.status_code == 429 and "per minute" in info.value.detail


def test_daily_limit_counts_audit_rows(tmp_path) -> None:
    audit = AuditLog(tmp_path / "k.db")
    store = ApiKeyStore(tmp_path / "k.db", audit=audit)
    key = store.create("daily", scopes="memory:read", daily_limit=2)
    store.authorize(key)
    audit.record("daily", "POST", "/v1/memory/context", 200, None, None)
    audit.record("daily", "POST", "/v1/memory/context", 200, None, None)
    with pytest.raises(Denied) as info:
        store.authorize(key)
    assert info.value.status_code == 429 and "daily quota" in info.value.detail


def test_set_limits_and_zero_removes_them(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "k.db")
    store.create("l", scopes="memory:read")
    assert store.set_limits("l", daily_limit=5, per_minute=3)
    row = next(r for r in store.list() if r["name"] == "l")
    assert (row["daily_limit"], row["per_minute"]) == (5, 3)
    assert store.set_limits("l", daily_limit=0)
    row = next(r for r in store.list() if r["name"] == "l")
    assert row["daily_limit"] is None and row["per_minute"] == 3


# ---- audit log ---------------------------------------------------------------------

def test_every_request_is_audited_with_key_and_status(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/memory/context", json={"query": "x"}, headers=AUTH).status_code == 200
        assert client.post("/v1/memory/context", json={"query": "x"}, headers={"Authorization": "Bearer mag_wrong"}).status_code == 401
        audit = AuditLog(settings.memory.resolved_db_path())
        rows = audit.query(hours=1)
    ok = next(row for row in rows if row["status"] == 200 and row["path"] == "/v1/memory/context")
    denied = next(row for row in rows if row["status"] == 401)
    assert ok["key_name"] == "test" and ok["method"] == "POST" and ok["client"]
    assert denied["key_name"] == "-"  # unknown credentials are logged too


def test_health_is_not_audited(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        client.get("/health")
        assert not AuditLog(settings.memory.resolved_db_path()).query(hours=1)


def test_mcp_tool_name_is_in_the_audit_line(settings, fake_llm) -> None:
    store, _ = _store(settings)
    key = store.create("readonly", scopes="memory:read")
    with TestClient(create_app(settings)) as client:
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ask", "arguments": {"prompt": "x"}}}
        assert client.post("/mcp/", json=body, headers={**MCP_ACCEPT, "Authorization": f"Bearer {key}"}).status_code == 403
        rows = AuditLog(settings.memory.resolved_db_path()).query(key="readonly", hours=1)
    assert rows and rows[0]["tool"] == "ask" and rows[0]["status"] == 403


def test_audit_purge_removes_old_rows(tmp_path) -> None:
    audit = AuditLog(tmp_path / "a.db")
    audit.record("k", "GET", "/x", 200, None, None)
    with sqlite3.connect(tmp_path / "a.db") as connection:
        connection.execute("UPDATE api_audit SET ts = ts - 400 * 86400")
    assert audit.purge(365) == 1
    assert audit.query(hours=None) == []


# ---- soft-deleted memory -----------------------------------------------------------

def test_soft_deleted_rows_are_invisible_but_kept(tmp_path) -> None:
    memory = GraphMemory(tmp_path / "m.db")
    asyncio.run(memory.initialize())
    asyncio.run(memory.add_node("s", "fact", "Payments use Stripe webhooks"))
    asyncio.run(memory.add_node("s", "fact", "Orders ship from Berlin"))

    removed = asyncio.run(memory.forget("s", match="Stripe", actor="laptop", reason="wrong"))
    assert len(removed) == 1
    assert all("Stripe" not in row["content"] for row in asyncio.run(memory.fetch_context("s", "Stripe", related_limit=5)))
    assert all("Stripe" not in row["content"] for row in asyncio.run(memory.fetch_context("s", "")))
    assert asyncio.run(memory.stats())["nodes"] == 1

    deleted = asyncio.run(memory.list_deleted("s"))
    assert len(deleted) == 1 and deleted[0]["deleted_by"] == "laptop" and deleted[0]["delete_reason"] == "wrong"
    with sqlite3.connect(tmp_path / "m.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2  # still on disk


def test_dry_run_forget_changes_nothing(tmp_path) -> None:
    memory = GraphMemory(tmp_path / "m.db")
    asyncio.run(memory.initialize())
    asyncio.run(memory.add_node("s", "fact", "Keep me please"))
    asyncio.run(memory.forget("s", match="Keep", dry_run=True, actor="x"))
    assert asyncio.run(memory.list_deleted()) == []


def test_purge_removes_only_old_soft_deletes(tmp_path) -> None:
    memory = GraphMemory(tmp_path / "m.db")
    asyncio.run(memory.initialize())
    asyncio.run(memory.add_node("s", "note", "Old note to remove"))
    asyncio.run(memory.add_node("s", "note", "Recent note to remove"))
    asyncio.run(memory.forget("s", match="Old note", actor="x"))
    with sqlite3.connect(tmp_path / "m.db") as connection:
        connection.execute("UPDATE nodes SET deleted_at = deleted_at - 400 * 86400 WHERE content LIKE 'Old%'")
    asyncio.run(memory.forget("s", match="Recent", actor="x"))
    assert asyncio.run(memory.purge_deleted(365)) == 1
    remaining = asyncio.run(memory.list_deleted())
    assert [row["content"] for row in remaining] == ["Recent note to remove"]


def test_old_database_gets_the_new_columns(tmp_path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:  # schema from before soft delete
        connection.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, "
            "content TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL)"
        )
        connection.execute("INSERT INTO nodes VALUES ('a', 's', 'fact', 'legacy fact', '{}', 1)")
    memory = GraphMemory(path)
    asyncio.run(memory.initialize())
    assert asyncio.run(memory.stats())["nodes"] == 1
    asyncio.run(memory.forget("s", match="legacy", actor="x"))
    assert asyncio.run(memory.stats())["nodes"] == 0


def test_forget_endpoint_records_the_acting_key(settings, fake_llm) -> None:
    store, _ = _store(settings)
    key = store.create("janitor", scopes="memory:write,memory:delete")
    with TestClient(create_app(settings)) as client:
        client.post("/v1/memory/events", json={"session_id": "p", "role": "fact", "content": "Delete this fact"}, headers=AUTH)
        response = client.post(
            "/v1/memory/forget",
            json={"session_id": "p", "match": "Delete this", "dry_run": False, "reason": "outdated"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200 and response.json()["deleted"] is True
    deleted = asyncio.run(GraphMemory(settings.memory.resolved_db_path()).list_deleted("p"))
    assert deleted and deleted[0]["deleted_by"] == "janitor" and deleted[0]["delete_reason"] == "outdated"
