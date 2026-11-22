"""Gateway API keys: one per client, stored as SHA-256 hashes, each with a set of scopes.

Scopes limit what a leaked key can do:
  memory:read    search and context lookups
  memory:write   record memory, hook events
  memory:delete  forget (remote deletion; off by default)
  models         chat completions and the MCP `ask` tool (these spend provider money)

A key with no scopes (for example one from before scopes existed) can do nothing.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import Any

KEY_PREFIX = "mag_"
TABLE = "gateway_api_keys"
SCOPES = ("memory:read", "memory:write", "memory:delete", "models")
DEFAULT_SCOPES = ("memory:read", "memory:write")


class ApiKeyError(ValueError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_scopes(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_SCOPES
    items = value.split(",") if isinstance(value, str) else list(value)
    scopes = tuple(dict.fromkeys(item.strip() for item in items if item.strip()))
    unknown = [scope for scope in scopes if scope not in SCOPES]
    if unknown:
        raise ApiKeyError(f"unknown scope(s): {', '.join(unknown)}; choose from {', '.join(SCOPES)}")
    if not scopes:
        raise ApiKeyError("a key needs at least one scope")
    return scopes


class ApiKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        name TEXT PRIMARY KEY,
                        key_hash TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL,
                        last_used_at REAL,
                        revoked INTEGER NOT NULL DEFAULT 0,
                        scopes TEXT NOT NULL DEFAULT ''
                    )"""
            )
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
            if "scopes" not in columns:  # databases created before scopes: fail closed, no scopes
                connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN scopes TEXT NOT NULL DEFAULT ''")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, name: str, scopes: str | Iterable[str] | None = None, value: str | None = None) -> str:
        """Create a key and return it. The caller shows it to the user now; it is not stored in plain text."""
        if not name or len(name) > 64 or not all(c.isalnum() or c in "-_." for c in name):
            raise ApiKeyError("name: 1-64 characters, letters, digits, '-', '_' or '.'")
        granted = ",".join(parse_scopes(scopes))
        key = value or KEY_PREFIX + secrets.token_urlsafe(32)
        with closing(self._connect()) as connection, connection:
            if connection.execute(f"SELECT 1 FROM {TABLE} WHERE name = ?", (name,)).fetchone():
                raise ApiKeyError(f"a key named '{name}' already exists; revoke it first or choose another name")
            connection.execute(
                f"INSERT INTO {TABLE} (name, key_hash, created_at, scopes) VALUES (?, ?, ?, ?)",
                (name, _digest(key), time.time(), granted),
            )
        return key

    def scopes_for(self, presented: str) -> frozenset[str] | None:
        """Scopes of an active key, or None when the key is unknown or revoked."""
        if not presented or not presented.startswith(KEY_PREFIX):
            return None
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f"SELECT name, scopes FROM {TABLE} WHERE key_hash = ? AND revoked = 0", (_digest(presented),)
            ).fetchone()
            if row is None:
                return None
            granted = frozenset(scope for scope in row["scopes"].split(",") if scope)
            if not granted:  # a key with no scopes (pre-scope databases) is treated as unknown
                return None
            connection.execute(f"UPDATE {TABLE} SET last_used_at = ? WHERE name = ?", (time.time(), row["name"]))
        return granted

    def verify(self, presented: str) -> bool:
        return self.scopes_for(presented) is not None

    def set_scopes(self, name: str, scopes: str | Iterable[str]) -> bool:
        granted = ",".join(parse_scopes(scopes))
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE {TABLE} SET scopes = ? WHERE name = ? AND revoked = 0", (granted, name))
            return cursor.rowcount > 0

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT name, created_at, last_used_at, revoked, scopes FROM {TABLE} ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, name: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE {TABLE} SET revoked = 1 WHERE name = ? AND revoked = 0", (name,))
            return cursor.rowcount > 0

    def active_count(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE revoked = 0").fetchone()[0]
