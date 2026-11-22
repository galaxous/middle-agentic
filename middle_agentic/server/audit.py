"""Request audit log: who (key name) called what (path, MCP tool), when, with what result.

Written by a pure ASGI middleware around the whole gateway. Authentication and the MCP guard fill in
the key name and tool name for the current request through a context variable, so the log line
can be written after the response is sent. Failed attempts are logged too (key "-").
"""
from __future__ import annotations

import calendar
import logging
import sqlite3
import time
from contextlib import closing
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger("middle_agentic.audit")

TABLE = "api_audit"
SKIP_PATHS = frozenset({"/health"})
_request_info: ContextVar[dict[str, Any] | None] = ContextVar("magent_request_info", default=None)


def note(**fields: Any) -> None:
    """Attach fields (key, tool) to the current request's audit entry. No-op outside a request."""
    info = _request_info.get()
    if info is not None:
        info.update({key: value for key, value in fields.items() if value is not None})


def current_key() -> str | None:
    info = _request_info.get()
    return info.get("key") if info else None


def _midnight_utc() -> float:
    today = time.gmtime()
    return float(calendar.timegm((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0)))


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        key_name TEXT NOT NULL,
                        method TEXT NOT NULL,
                        path TEXT NOT NULL,
                        tool TEXT,
                        status INTEGER NOT NULL,
                        client TEXT
                    )"""
            )
            connection.execute(f"CREATE INDEX IF NOT EXISTS idx_audit_key_ts ON {TABLE} (key_name, ts)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def record(self, key: str, method: str, path: str, status: int, client: str | None, tool: str | None) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"INSERT INTO {TABLE} (ts, key_name, method, path, tool, status, client) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), key, method, path, tool, status, client),
            )

    def count_since_midnight(self, key: str) -> int:
        with closing(self._connect()) as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM {TABLE} WHERE key_name = ? AND ts >= ?", (key, _midnight_utc())
            ).fetchone()[0]

    def query(self, key: str | None = None, hours: float | None = 24, limit: int = 100) -> list[dict[str, Any]]:
        clauses, params = [], []
        if key:
            clauses.append("key_name = ?")
            params.append(key)
        if hours is not None:
            clauses.append("ts >= ?")
            params.append(time.time() - hours * 3600)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT ts, key_name, method, path, tool, status, client FROM {TABLE} {where} "
                f"ORDER BY ts DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        with closing(self._connect()) as connection, connection:
            return connection.execute(f"DELETE FROM {TABLE} WHERE ts < ?", (cutoff,)).rowcount


class AuditMiddleware:
    """Outermost ASGI layer: one audit row per HTTP request (except the health probe)."""

    def __init__(self, app: Any, log: AuditLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["path"] in SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        info: dict[str, Any] = {}
        token = _request_info.set(info)
        status = {"code": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        client = scope.get("client")
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _request_info.reset(token)
            try:
                self.log.record(
                    key=info.get("key", "-"),
                    method=scope["method"],
                    path=scope["path"],
                    status=status["code"],
                    client=f"{client[0]}:{client[1]}" if client else None,
                    tool=info.get("tool"),
                )
            except Exception:  # noqa: BLE001 - auditing must never break a request
                logger.exception("could not write audit entry")

