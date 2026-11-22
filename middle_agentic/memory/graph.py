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
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "were", "you", "not", "but", "can", "how", "why", "has", "had",
    "its", "our", "who", "did", "does", "use", "any", "all", "get", "let",
    "this", "that", "with", "from", "have", "what", "when", "where", "which", "about", "would",
    "could", "should", "there", "their", "them", "then", "than", "into", "your", "yours", "just",
    "remind", "please", "tell",
    "bir", "ile", "ama", "ben", "sen", "biz", "siz", "var", "yok", "çok", "her", "şey", "mi", "mı",
    "için", "gibi", "daha", "olan", "nasıl", "neden", "bunu", "şunu", "veya", "nedir", "hangi",
    "neydi", "olarak", "şimdi", "sonra", "önce", "bana", "bize",
})

# Curated notes outrank raw conversation: their BM25 score is multiplied by this weight.
ROLE_WEIGHTS: dict[str, float] = {"decision": 2.0, "fact": 2.0}
IMPORTANT_ROLES = tuple(ROLE_WEIGHTS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    deleted_at  REAL,
    deleted_by  TEXT,
    delete_reason TEXT
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

# A standalone FTS table keyed by node id (not rowid): rowids of a TEXT-keyed table may change on VACUUM.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS nodes_fts_insert AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts (node_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
END;
CREATE TRIGGER IF NOT EXISTS nodes_fts_update AFTER UPDATE OF content ON nodes BEGIN
    DELETE FROM nodes_fts WHERE node_id = old.id;
    INSERT INTO nodes_fts (node_id, content) VALUES (new.id, new.content);
END;
"""


# Columns added after the first release; older databases get them on start-up.
_NODE_COLUMNS = {"deleted_at": "REAL", "deleted_by": "TEXT", "delete_reason": "TEXT"}


def _ensure_columns(connection: sqlite3.Connection) -> None:
    present = {row[1] for row in connection.execute("PRAGMA table_info(nodes)")}
    for name, kind in _NODE_COLUMNS.items():
        if name not in present:
            connection.execute(f"ALTER TABLE nodes ADD COLUMN {name} {kind}")
    # Created here, not in SCHEMA: on an old database the column only exists after the ALTER above.
    connection.execute("CREATE INDEX IF NOT EXISTS idx_nodes_deleted ON nodes (deleted_at)")


LIVE = "deleted_at IS NULL"  # every read path filters on this; soft-deleted rows stay for investigation


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    keywords: list[str] = []
    for word in re.findall(r"[^\W_]{3,}", text.lower()):
        if word not in STOPWORDS and word not in keywords:
            keywords.append(word)
        if len(keywords) >= limit:
            break
    return keywords


def build_fts_query(text: str) -> str | None:
    """OR of prefix terms. Long words are trimmed so inflected forms match (veritabanında -> veritabanı*)."""
    terms: list[str] = []
    for word in extract_keywords(text):
        stem = word[: max(4, len(word) - 3)] if len(word) > 6 else word
        term = f'"{stem}"*'
        if term not in terms:
            terms.append(term)
    return " OR ".join(terms) or None


def normalise_session_id(raw: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._:@-]", "", (raw or "").strip())[:128]
    return cleaned or "default"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def format_context(rows: Sequence[dict[str, Any]], snippet_chars: int = 500) -> str | None:
    """Render memory nodes as the text block injected into prompts and agent hooks."""
    if not rows:
        return None

    def render(row: dict[str, Any]) -> str:
        snippet = " ".join(str(row["content"]).split())
        if len(snippet) > snippet_chars:
            snippet = snippet[:snippet_chars] + "..."
        return f"- [{row['role']}] {snippet}"

    important = [render(row) for row in rows if row["role"] in IMPORTANT_ROLES]
    others = [render(row) for row in rows if row["role"] not in IMPORTANT_ROLES]
    sections = ["Relevant memory from earlier interactions (may be outdated, use only if helpful):"]
    if important:
        sections.append("Project decisions and facts:\n" + "\n".join(important))
    if others:
        sections.append("Related conversation:\n" + "\n".join(others))
    return "\n".join(sections)


class GraphMemory:
    def __init__(
        self, db_path: Path, recent_limit: int = 6, related_limit: int = 4, important_limit: int = 5
    ) -> None:
        self.db_path = db_path
        self.recent_limit = recent_limit
        self.related_limit = related_limit
        self.important_limit = important_limit
        self.fts_enabled = False

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
            _ensure_columns(connection)
            connection.commit()
            had_fts = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes_fts'"
            ).fetchone() is not None
            try:
                connection.executescript(FTS_SCHEMA)
            except sqlite3.OperationalError as exc:
                logger.warning("SQLite FTS5 unavailable (%s); memory search falls back to LIKE", exc)
                return
            if not had_fts:
                connection.execute("INSERT INTO nodes_fts (node_id, content) SELECT id, content FROM nodes")
            connection.commit()
            self.fts_enabled = True

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    # -- read -----------------------------------------------------------------------
    def _search_sync(
        self, connection: sqlite3.Connection, session_id: str, query: str, limit: int
    ) -> list[sqlite3.Row]:
        if self.fts_enabled:
            match = build_fts_query(query)
            if not match:
                return []
            weights = " ".join("WHEN ? THEN ?" for _ in ROLE_WEIGHTS)
            weight_params = [value for item in ROLE_WEIGHTS.items() for value in item]
            try:
                return connection.execute(
                    f"SELECT n.id, n.role, n.content, n.created_at, "
                    f"bm25(nodes_fts) * (CASE n.role {weights} ELSE 1.0 END) AS score "
                    f"FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.node_id "
                    f"WHERE nodes_fts MATCH ? AND n.session_id = ? AND n.deleted_at IS NULL "
                    f"ORDER BY score, n.created_at DESC LIMIT ?",
                    (*weight_params, match, session_id, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning("FTS query failed (%s); falling back to LIKE", exc)

        keywords = extract_keywords(query)
        if not keywords:
            return []
        like_clause = " OR ".join("content LIKE ? ESCAPE '\\'" for _ in keywords)
        marks = ",".join("?" for _ in IMPORTANT_ROLES)
        return connection.execute(
            f"SELECT id, role, content, created_at FROM nodes "
            f"WHERE session_id = ? AND deleted_at IS NULL AND ({like_clause}) "
            f"ORDER BY role IN ({marks}) DESC, created_at DESC LIMIT ?",
            (session_id, *[f"%{_escape_like(word)}%" for word in keywords], *IMPORTANT_ROLES, limit),
        ).fetchall()

    @staticmethod
    def _responded_neighbours(connection: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in ids)
        return connection.execute(
            f"SELECT n.id, n.role, n.content, n.created_at FROM edges e "
            f"JOIN nodes n ON n.id = CASE WHEN e.src_id IN ({marks}) THEN e.dst_id ELSE e.src_id END "
            f"WHERE e.relation = 'responded_by' AND n.deleted_at IS NULL "
            f"AND (e.src_id IN ({marks}) OR e.dst_id IN ({marks}))",
            (*ids, *ids, *ids),
        ).fetchall()

    def _context_sync(
        self, session_id: str, query: str, recent_limit: int, related_limit: int, important_limit: int
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            recent = connection.execute(
                "SELECT id, role, content, created_at FROM nodes "
                "WHERE session_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (session_id, recent_limit),
            ).fetchall()
            selected: dict[str, sqlite3.Row] = {row["id"]: row for row in recent}

            # Without a query (session start) the latest decisions and facts are the best summary.
            if not build_fts_query(query) and important_limit > 0:
                marks = ",".join("?" for _ in IMPORTANT_ROLES)
                important = connection.execute(
                    f"SELECT id, role, content, created_at FROM nodes "
                    f"WHERE session_id = ? AND deleted_at IS NULL AND role IN ({marks}) ORDER BY created_at DESC LIMIT ?",
                    (session_id, *IMPORTANT_ROLES, important_limit),
                ).fetchall()
                selected.update({row["id"]: row for row in important})

            if related_limit > 0:
                related = self._search_sync(connection, session_id, query, related_limit)
                new_related = [row for row in related if row["id"] not in selected]
                selected.update({row["id"]: row for row in new_related})

                # One-hop expansion: a matched question brings its answer along, and vice versa.
                if new_related:
                    neighbours = self._responded_neighbours(connection, [row["id"] for row in new_related])
                    selected.update({row["id"]: row for row in neighbours})

        rows = sorted(selected.values(), key=lambda row: row["created_at"])
        return [{key: row[key] for key in ("id", "role", "content", "created_at")} for row in rows]

    async def fetch_context(
        self,
        session_id: str,
        query: str,
        recent_limit: int | None = None,
        related_limit: int | None = None,
        important_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._context_sync,
            session_id,
            query,
            self.recent_limit if recent_limit is None else recent_limit,
            self.related_limit if related_limit is None else related_limit,
            self.important_limit if important_limit is None else important_limit,
        )

    def _session_graph_sync(self, session_id: str, limit: int) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            nodes = connection.execute(
                "SELECT id, role, content, metadata, created_at FROM nodes "
                "WHERE session_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
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
                "WHERE deleted_at IS NULL GROUP BY session_id ORDER BY last_activity DESC LIMIT ?",
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
                "SELECT id, role FROM nodes WHERE session_id = ? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1",
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
                    "SELECT id FROM nodes WHERE session_id = ? AND role = 'user' AND id != ? AND deleted_at IS NULL "
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

    # -- delete (soft) -------------------------------------------------------------
    # forget marks nodes as deleted (who, when, why). Nothing is removed from the database, so a
    # later investigation can read them with list_deleted. Every read path filters them out.
    def _forget_sync(
        self,
        session_id: str,
        ids: Sequence[str],
        match: str | None,
        role: str | None,
        with_answers: bool,
        dry_run: bool,
        actor: str | None,
        reason: str | None,
    ) -> list[dict[str, Any]]:
        if not ids and not match:
            raise ValueError("give node ids or a text to match")
        clauses, params = ["session_id = ?", LIVE], [session_id]
        if ids:
            clauses.append(f"id IN ({','.join('?' for _ in ids)})")
            params.extend(ids)
        if match:
            clauses.append("content LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(match)}%")
        if role:
            clauses.append("role = ?")
            params.append(role)

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT id, role, content, created_at FROM nodes WHERE {' AND '.join(clauses)}", params
            ).fetchall()
            selected = {row["id"]: row for row in rows}
            if with_answers and selected:
                neighbours = self._responded_neighbours(connection, list(selected))
                selected.update({row["id"]: row for row in neighbours})
            if selected and not dry_run:
                marks = ",".join("?" for _ in selected)
                connection.execute(
                    f"UPDATE nodes SET deleted_at = ?, deleted_by = ?, delete_reason = ? WHERE id IN ({marks})",
                    [time.time(), actor, reason, *selected],
                )

        rows = sorted(selected.values(), key=lambda row: row["created_at"])
        return [{key: row[key] for key in ("id", "role", "content", "created_at")} for row in rows]

    async def forget(
        self,
        session_id: str,
        ids: Sequence[str] = (),
        match: str | None = None,
        role: str | None = None,
        with_answers: bool = True,
        dry_run: bool = False,
        actor: str | None = None,
        reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """Soft-delete nodes by id and/or content substring. A matched question takes its answer with it."""
        return await asyncio.to_thread(
            self._forget_sync, session_id, ids, match, role, with_answers, dry_run, actor, reason
        )

    def _list_deleted_sync(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        clauses, params = ["deleted_at IS NOT NULL"], []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT id, session_id, role, content, created_at, deleted_at, deleted_by, delete_reason "
                f"FROM nodes WHERE {' AND '.join(clauses)} ORDER BY deleted_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_deleted(self, session_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Soft-deleted nodes, newest deletion first. For investigation; not used by any read path."""
        return await asyncio.to_thread(self._list_deleted_sync, session_id, limit)

    def _purge_sync(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM nodes WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)
            )
            return cursor.rowcount

    async def purge_deleted(self, older_than_days: float) -> int:
        """Hard-delete nodes that were soft-deleted more than `older_than_days` ago (retention)."""
        if older_than_days < 0:
            raise ValueError("older_than_days must be 0 or more")
        return await asyncio.to_thread(self._purge_sync, older_than_days)

    # -- stats ----------------------------------------------------------------------
    def _stats_sync(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            nodes = connection.execute("SELECT COUNT(*) FROM nodes WHERE deleted_at IS NULL").fetchone()[0]
            edges = connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            sessions = connection.execute("SELECT COUNT(DISTINCT session_id) FROM nodes WHERE deleted_at IS NULL").fetchone()[0]
        return {"nodes": nodes, "edges": edges, "sessions": sessions}

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats_sync)
