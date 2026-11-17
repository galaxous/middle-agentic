"""Lightweight graph memory on SQLite.

nodes: one row per message or event, scoped by ``session_id``.
edges: typed relations between nodes:
    follows       previous node in the session  -> new node
    responded_by  user node                      -> assistant answer
    <custom>      any relation given through the memory API (``link_to`` + ``relation``)

All public methods are async and run the blocking sqlite3 calls in a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "what", "when", "where", "which", "about", "would",
    "could", "should", "there", "their", "them", "then", "than", "into", "your", "yours", "just",
    "için", "gibi", "daha", "olan", "nasıl", "neden", "bunu", "şunu", "veya",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_session_time ON nodes (session_id, created_at);

CREATE TABLE IF NOT EXISTS edges (
    src_id      TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    dst_id      TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (src_id, dst_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst_id);
"""


def extract_keywords(text: str, limit: int = 5) -> list[str]:
    keywords: list[str] = []
    for word in re.findall(r"\w{4,}", text.lower()):
        if word not in STOPWORDS and word not in keywords:
            keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def normalise_session_id(raw: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:@-]", "", (raw or "").strip())[:128]
    return cleaned or "default"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def format_context(rows: Sequence[dict[str, Any]], snippet_chars: int = 500) -> str | None:
    """Render memory nodes as the text block injected into prompts and agent hooks."""
    if not rows:
        return None
    lines = []
    for row in rows:
        snippet = " ".join(str(row["content"]).split())
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars] + "..."
        lines.append(f"- [{row['role']}] {snippet}")
    return (
        "Relevant memory from earlier interactions (may be outdated, use only if helpful):\n"
        + "\n".join(lines)
    )


class GraphMemory:
    def __init__(self, db_path: Path, recent_limit: int = 6, related_limit: int = 4) -> None:
        self.db_path = db_path
        self.recent_limit = recent_limit
        self.related_limit = related_limit

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    # -- schema ---------------------------------------------------------------------
    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            # WAL lets the background writer and request readers work concurrently.
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.commit()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    # -- read -----------------------------------------------------------------------
    def _context_sync(self, session_id: str, query: str, recent_limit: int, related_limit: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            recent = connection.execute(
                "SELECT id, role, content, created_at FROM nodes "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, recent_limit),
            ).fetchall()
            selected: dict[str, sqlite3.Row] = {row["id"]: row for row in recent}

            keywords = extract_keywords(query)
            if keywords and related_limit > 0:
                like_clause = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in keywords)
                related = connection.execute(
                    f"SELECT id, role, content, created_at FROM nodes "
                    f"WHERE session_id = ? AND ({like_clause}) "
                    f"ORDER BY created_at DESC LIMIT ?",
                    (session_id, *[f"%{_escape_like(word)}%" for word in keywords], related_limit),
                ).fetchall()
                new_related = [row for row in related if row["id"] not in selected]
                selected.update({row["id"]: row for row in new_related})

                # One-hop expansion: a matched question brings its answer along, and vice versa.
                if new_related:
                    ids = [row["id"] for row in new_related]
                    marks = ",".join("?" for _ in ids)
                    neighbours = connection.execute(
                        f"SELECT n.id, n.role, n.content, n.created_at FROM edges e "
                        f"JOIN nodes n ON n.id = CASE WHEN e.src_id IN ({marks}) THEN e.dst_id ELSE e.src_id END "
                        f"WHERE e.relation = 'responded_by' AND (e.src_id IN ({marks}) OR e.dst_id IN ({marks}))",
                        (*ids, *ids, *ids),
                    ).fetchall()
                    selected.update({row["id"]: row for row in neighbours})

        return [dict(row) for row in sorted(selected.values(), key=lambda row: row["created_at"])]

    async def fetch_context(
        self, session_id: str, query: str, recent_limit: int | None = None, related_limit: int | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._context_sync,
            session_id,
            query,
            self.recent_limit if recent_limit is None else recent_limit,
            self.related_limit if related_limit is None else related_limit,
        )

    def _session_graph_sync(self, session_id: str, limit: int) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            nodes = connection.execute(
                "SELECT id, role, content, metadata, created_at FROM nodes "
                "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            ids = [row["id"] for row in nodes]
            edges: list[sqlite3.Row] = []
            if ids:
                marks = ",".join("?" for _ in ids)
                edges = connection.execute(
                    f"SELECT src_id, dst_id, relation, created_at FROM edges "
                    f"WHERE src_id IN ({marks}) AND dst_id IN ({marks})",
                    (*ids, *ids),
                ).fetchall()
        return {
            "session_id": session_id,
            "nodes": [
                {**dict(row), "metadata": json.loads(row["metadata"] or "{}")}
                for row in reversed(nodes)
            ],
            "edges": [dict(row) for row in edges],
        }

    async def session_graph(self, session_id: str, limit: int = 200) -> dict[str, Any]:
        return await asyncio.to_thread(self._session_graph_sync, session_id, limit)

    def _list_sessions_sync(self, limit: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT session_id, COUNT(*) AS nodes, MAX(created_at) AS last_activity FROM nodes "
                "GROUP BY session_id ORDER BY last_activity DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sessions_sync, limit)

    # -- write ----------------------------------------------------------------------
    def _add_node_sync(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any],
        link_to: str | None,
        relation: str | None,
    ) -> str:
        now = time.time()
        node_id = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            previous = connection.execute(
                "SELECT id, role FROM nodes WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            # Keep strict ordering even when two writes share the same clock tick.
            last_time = connection.execute(
                "SELECT MAX(created_at) FROM nodes WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            created_at = max(now, (last_time or 0) + 1e-6)

            connection.execute(
                "INSERT INTO nodes (id, session_id, role, content, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, session_id, role, content, json.dumps(metadata, default=str), created_at),
            )

            edges: list[tuple[str, str, str, float]] = []
            if previous is not None:
                edges.append((previous["id"], node_id, "follows", created_at))
            if link_to:
                edges.append((link_to, node_id, relation or "related_to", created_at))
            elif role == "assistant":
                last_user = connection.execute(
                    "SELECT id FROM nodes WHERE session_id = ? AND role = 'user' AND id != ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (session_id, node_id),
                ).fetchone()
                if last_user is not None:
                    edges.append((last_user["id"], node_id, "responded_by", created_at))
            connection.executemany(
                "INSERT OR IGNORE INTO edges (src_id, dst_id, relation, created_at) VALUES (?, ?, ?, ?)",
                edges,
            )
        return node_id

    async def add_node(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        link_to: str | None = None,
        relation: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._add_node_sync, session_id, role, content, metadata or {}, link_to, relation
        )

    async def record_exchange(
        self, session_id: str, user_text: str, assistant_text: str, metadata: dict[str, Any]
    ) -> tuple[str, str]:
        user_id = await self.add_node(session_id, "user", user_text)
        assistant_id = await self.add_node(
            session_id, "assistant", assistant_text, metadata, link_to=user_id, relation="responded_by"
        )
        return user_id, assistant_id

    # -- stats ----------------------------------------------------------------------
    def _stats_sync(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            sessions = connection.execute("SELECT COUNT(DISTINCT session_id) FROM nodes").fetchone()[0]
        return {"nodes": nodes, "edges": edges, "sessions": sessions}

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)
