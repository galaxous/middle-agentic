"""Gateway API keys: one per client, stored as SHA-256 hashes, with scopes, expiry and quotas.

Scopes limit what a leaked key can do:
  memory:read    search and context lookups
  memory:write   record memory, hook events
  memory:delete  forget (soft delete; off by default)
  models         chat completions and the MCP `ask` tool (these spend provider money)

Each key also has an expiry (default 90 days, 0 = never), an optional requests-per-minute limit
(counted in this process) and an optional requests-per-day limit (counted in the audit log).
A key without scopes is treated as unknown.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from middle_agentic.server.audit import AuditLog

KEY_PREFIX = "mag_"
TABLE = "gateway_api_keys"
SCOPES = ("memory:read", "memory:write", "memory:delete", "models")
DEFAULT_SCOPES = ("memory:read", "memory:write")
DEFAULT_EXPIRY_DAYS = 90
_KEY_COLUMNS = {"scopes": "TEXT NOT NULL DEFAULT ''", "expires_at": "REAL", "daily_limit": "INTEGER", "per_minute": "INTEGER"}


class ApiKeyError(ValueError):
    pass


class Denied(Exception):
    """Request refused before reaching a handler. status_code is 401 (who you are) or 429 (too many)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class KeyRecord:
    name: str
    scopes: frozenset[str]
    expires_at: float | None
    daily_limit: int | None
    per_minute: int | None


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


def _expiry_from_days(days: float | None) -> float | None:
    if days is None or days == 0:
        return None
    if days < 0:
        raise ApiKeyError("expiry must be 0 (never) or a positive number of days")
    return time.time() + days * 86400


class ApiKeyStore:
    def __init__(self, path: Path, audit: "AuditLog | None" = None) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.audit = audit
        self._minute_window: dict[str, deque[float]] = defaultdict(deque)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        name TEXT PRIMARY KEY,
                        key_hash TEXT NOT NULL UNIQUE,
                        created_at REAL NOT NULL,
                        last_used_at REAL,
                        revoked INTEGER NOT NULL DEFAULT 0,
                        scopes TEXT NOT NULL DEFAULT '',
                        expires_at REAL,
                        daily_limit INTEGER,
                        per_minute INTEGER
                    )"""
            )
            present = {row["name"] for row in connection.execute(f"PRAGMA table_info({TABLE})")}
            for column, kind in _KEY_COLUMNS.items():
                # Databases from earlier versions: no scopes means no access (fail closed); no expiry means never.
                if column not in present:
                    connection.execute(f"ALTER TABLE {TABLE} ADD COLUMN {column} {kind}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def create(
        self,
        name: str,
        scopes: str | Iterable[str] | None = None,
        value: str | None = None,
        expires_days: float | None = DEFAULT_EXPIRY_DAYS,
        daily_limit: int | None = None,
        per_minute: int | None = None,
    ) -> str:
        """Create a key and return it. The caller shows it to the user now; only its hash is stored."""
        if not name or len(name) > 64 or not all(c.isalnum() or c in "-_." for c in name):
            raise ApiKeyError("name: 1-64 characters, letters, digits, '-', '_' or '.'")
        granted = ",".join(parse_scopes(scopes))
        expires_at = _expiry_from_days(expires_days)
        _check_limits(daily_limit, per_minute)
        key = value or KEY_PREFIX + secrets.token_urlsafe(32)
        with closing(self._connect()) as connection, connection:
            if connection.execute(f"SELECT 1 FROM {TABLE} WHERE name = ?", (name,)).fetchone():
                raise ApiKeyError(f"a key named '{name}' already exists; revoke it first or choose another name")
            connection.execute(
                f"INSERT INTO {TABLE} (name, key_hash, created_at, scopes, expires_at, daily_limit, per_minute) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, _digest(key), time.time(), granted, expires_at, daily_limit or None, per_minute or None),
            )
        return key

    def _row(self, presented: str) -> sqlite3.Row | None:
        if not presented or not presented.startswith(KEY_PREFIX):
            return None
        with closing(self._connect()) as connection:
            return connection.execute(
                f"SELECT name, scopes, expires_at, daily_limit, per_minute, revoked FROM {TABLE} WHERE key_hash = ?",
                (_digest(presented),),
            ).fetchone()

    def authenticate(self, presented: str) -> KeyRecord | None:
        """The key's record if it is known, active, unexpired and has scopes. No quota is applied here."""
        row = self._row(presented)
        if row is None or row["revoked"]:
            return None
        if row["expires_at"] is not None and row["expires_at"] <= time.time():
            return None
        scopes = frozenset(scope for scope in row["scopes"].split(",") if scope)
        if not scopes:
            return None
        with closing(self._connect()) as connection, connection:
            connection.execute(f"UPDATE {TABLE} SET last_used_at = ? WHERE name = ?", (time.time(), row["name"]))
        return KeyRecord(row["name"], scopes, row["expires_at"], row["daily_limit"], row["per_minute"])

    def authorize(self, presented: str) -> KeyRecord:
        """authenticate plus quotas. Raises Denied(401) for unknown/revoked/expired keys, Denied(429) over quota."""
        record = self.authenticate(presented)
        if record is None:
            row = self._row(presented)
            if row is not None and not row["revoked"] and row["expires_at"] is not None and row["expires_at"] <= time.time():
                raise Denied(401, "API key expired; ask the gateway admin for a new one (magent key renew)")
            raise Denied(401, "Invalid or missing API key")
        if record.per_minute:
            window = self._minute_window[record.name]
            now = time.time()
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= record.per_minute:
                raise Denied(429, f"rate limit: more than {record.per_minute} requests per minute for this key")
            window.append(now)
        if record.daily_limit and self.audit is not None:
            if self.audit.count_since_midnight(record.name) >= record.daily_limit:
                raise Denied(429, f"daily quota of {record.daily_limit} requests reached for this key")
        return record

    def scopes_for(self, presented: str) -> frozenset[str] | None:
        record = self.authenticate(presented)
        return record.scopes if record else None

    def verify(self, presented: str) -> bool:
        return self.authenticate(presented) is not None

    def set_scopes(self, name: str, scopes: str | Iterable[str]) -> bool:
        granted = ",".join(parse_scopes(scopes))
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE {TABLE} SET scopes = ? WHERE name = ? AND revoked = 0", (granted, name))
            return cursor.rowcount > 0

    def renew(self, name: str, expires_days: float) -> bool:
        expires_at = _expiry_from_days(expires_days)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE {TABLE} SET expires_at = ? WHERE name = ? AND revoked = 0", (expires_at, name)
            )
            return cursor.rowcount > 0

    def set_limits(self, name: str, daily_limit: int | None = None, per_minute: int | None = None) -> bool:
        """None leaves a limit unchanged; 0 removes it."""
        _check_limits(daily_limit, per_minute)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(f"SELECT daily_limit, per_minute FROM {TABLE} WHERE name = ? AND revoked = 0", (name,)).fetchone()
            if row is None:
                return False
            new_daily = row["daily_limit"] if daily_limit is None else (daily_limit or None)
            new_minute = row["per_minute"] if per_minute is None else (per_minute or None)
            connection.execute(f"UPDATE {TABLE} SET daily_limit = ?, per_minute = ? WHERE name = ?", (new_daily, new_minute, name))
            return True

    def list(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT name, created_at, last_used_at, revoked, scopes, expires_at, daily_limit, per_minute "
                f"FROM {TABLE} ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke(self, name: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"UPDATE {TABLE} SET revoked = 1 WHERE name = ? AND revoked = 0", (name,))
            return cursor.rowcount > 0

    def active_count(self) -> int:
        with closing(self._connect()) as connection:
            return connection.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE revoked = 0").fetchone()[0]


def _check_limits(daily_limit: int | None, per_minute: int | None) -> None:
    for label, value in (("daily limit", daily_limit), ("per-minute limit", per_minute)):
        if value is not None and value < 0:
            raise ApiKeyError(f"{label} must be 0 (no limit) or more")
