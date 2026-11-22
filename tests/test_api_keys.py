"""Per-client gateway keys: stored as hashes, checked on every request, revocable at once."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from middle_agentic.cli import main
from middle_agentic.server.api_keys import ApiKeyError, ApiKeyStore
from middle_agentic.server.app import create_app

from conftest import AUTH


def test_create_returns_key_once_and_stores_only_hash(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "keys.db")
    key = store.create("macbook-cursor")
    assert key.startswith("mag_") and len(key) > 30
    assert store.verify(key)

    with sqlite3.connect(tmp_path / "keys.db") as connection:
        stored = " ".join(str(value) for row in connection.execute("SELECT * FROM gateway_api_keys") for value in row)
    assert key not in stored  # only the SHA-256 digest is on disk


def test_duplicate_name_and_bad_name_are_refused(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "keys.db")
    store.create("pi")
    with pytest.raises(ApiKeyError, match="already exists"):
        store.create("pi")
    with pytest.raises(ApiKeyError):
        store.create("has space")


def test_revoke_takes_effect_immediately(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "keys.db")
    key = store.create("acer")
    assert store.revoke("acer") is True
    assert store.verify(key) is False
    assert store.revoke("acer") is False  # already revoked
    assert store.active_count() == 0


def test_wrong_or_empty_keys_are_refused(tmp_path) -> None:
    store = ApiKeyStore(tmp_path / "keys.db")
    store.create("x")
    assert not store.verify("")
    assert not store.verify("mag_guess")
    assert not store.verify("plainwithoutprefix")


def test_gateway_rejects_requests_without_a_known_key(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["auth_required"] is True
        assert client.post("/v1/memory/context", json={}).status_code == 401
        assert client.post("/v1/memory/context", json={}, headers={"Authorization": "Bearer mag_nope"}).status_code == 401
        assert client.post("/v1/memory/context", json={}, headers=AUTH).status_code == 200


def test_revoked_key_stops_working_on_the_gateway(settings, fake_llm) -> None:
    store = ApiKeyStore(settings.memory.resolved_db_path())
    key = store.create("laptop")
    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": f"Bearer {key}"}
        assert client.post("/v1/memory/context", json={}, headers=headers).status_code == 200
        store.revoke("laptop")
        assert client.post("/v1/memory/context", json={}, headers=headers).status_code == 401


def test_cli_key_lifecycle(monkeypatch, tmp_path, capsys, settings) -> None:
    from middle_agentic import cli

    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: settings)
    assert main(["key", "create", "cli-client"]) == 0
    out = capsys.readouterr().out
    key = [line for line in out.splitlines() if line.startswith("mag_")][0]
    assert ApiKeyStore(settings.memory.resolved_db_path()).verify(key)

    assert main(["key", "list"]) == 0
    assert "cli-client" in capsys.readouterr().out
    assert main(["key", "revoke", "cli-client"]) == 0
    assert not ApiKeyStore(settings.memory.resolved_db_path()).verify(key)
    assert main(["key", "revoke", "cli-client"]) != 0


def test_mcp_endpoint_uses_the_same_keys(settings, fake_llm) -> None:
    with TestClient(create_app(settings)) as client:
        init = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
        accept = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert client.post("/mcp/", json=init, headers=accept).status_code == 401
        assert client.post("/mcp/", json=init, headers={**accept, **AUTH}).status_code == 200
