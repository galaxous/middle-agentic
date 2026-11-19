from __future__ import annotations

import sqlite3

import pytest

from middle_agentic.memory.graph import (
    GraphMemory,
    build_fts_query,
    extract_keywords,
    format_context,
    normalise_session_id,
)


@pytest.fixture
async def memory(tmp_path) -> GraphMemory:
    graph = GraphMemory(tmp_path / "memory.db", recent_limit=2, related_limit=4)
    await graph.initialize()
    return graph


async def test_record_exchange_creates_follow_and_response_edges(memory: GraphMemory) -> None:
    user_id, assistant_id = await memory.record_exchange("s1", "question", "answer", {"tier": "local"})
    graph = await memory.session_graph("s1")
    assert [node["role"] for node in graph["nodes"]] == ["user", "assistant"]
    assert graph["nodes"][1]["metadata"] == {"tier": "local"}
    relations = {(edge["src_id"], edge["dst_id"], edge["relation"]) for edge in graph["edges"]}
    assert (user_id, assistant_id, "follows") in relations
    assert (user_id, assistant_id, "responded_by") in relations


async def test_assistant_node_links_to_last_user_node(memory: GraphMemory) -> None:
    user_id = await memory.add_node("s1", "user", "prompt")
    assistant_id = await memory.add_node("s1", "assistant", "reply")
    graph = await memory.session_graph("s1")
    assert {"src_id": user_id, "dst_id": assistant_id, "relation": "responded_by"} in [
        {key: edge[key] for key in ("src_id", "dst_id", "relation")} for edge in graph["edges"]
    ]


async def test_context_includes_keyword_match_and_its_answer(memory: GraphMemory) -> None:
    await memory.record_exchange("s1", "Which database for the ledger?", "Use Postgres with row level security", {})
    for index in range(4):
        await memory.record_exchange("s1", f"unrelated question {index}", f"unrelated answer {index}", {})

    rows = await memory.fetch_context("s1", "remind me about the ledger database")
    contents = [row["content"] for row in rows]
    # recent_limit=2 keeps only the last exchange; the keyword hit pulls in the old Q and its answer.
    assert "Which database for the ledger?" in contents
    assert "Use Postgres with row level security" in contents
    assert "unrelated answer 3" in contents
    assert "unrelated question 0" not in contents
    assert rows == sorted(rows, key=lambda row: row["created_at"])


async def test_sessions_are_isolated(memory: GraphMemory) -> None:
    await memory.add_node("a", "note", "alpha secret")
    assert await memory.fetch_context("b", "alpha secret") == []
    sessions = await memory.list_sessions()
    assert [s["session_id"] for s in sessions] == ["a"]


async def test_link_to_unknown_node_fails(memory: GraphMemory) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await memory.add_node("s1", "note", "x", link_to="missing")


async def test_custom_relation(memory: GraphMemory) -> None:
    decision = await memory.add_node("s1", "decision", "use postgres")
    note = await memory.add_node("s1", "note", "migrated", link_to=decision, relation="implements")
    edges = (await memory.session_graph("s1"))["edges"]
    assert any(e["src_id"] == decision and e["dst_id"] == note and e["relation"] == "implements" for e in edges)


async def test_like_wildcards_are_escaped(memory: GraphMemory) -> None:
    await memory.add_node("s1", "note", "plain text")
    rows = await memory.fetch_context("s1", "100%_____", recent_limit=0)
    assert rows == []


async def test_decisions_and_facts_outrank_conversation(tmp_path) -> None:
    memory = GraphMemory(tmp_path / "memory.db", recent_limit=0, related_limit=1)
    await memory.initialize()
    await memory.add_node("s1", "decision", "Use redis for caching sessions")
    await memory.add_node("s1", "user", "we talked about redis caching today")
    rows = await memory.fetch_context("s1", "redis caching")
    assert [row["role"] for row in rows] == ["decision"]


async def test_inflected_turkish_words_match(memory: GraphMemory) -> None:
    await memory.add_node("s1", "fact", "Veritabanı olarak PostgreSQL kullanılıyor")
    rows = await memory.fetch_context("s1", "veritabanında hangi sistem var", recent_limit=0)
    assert [row["content"] for row in rows] == ["Veritabanı olarak PostgreSQL kullanılıyor"]


async def test_session_start_lists_latest_decisions_and_facts(tmp_path) -> None:
    memory = GraphMemory(tmp_path / "memory.db", recent_limit=1, related_limit=0, important_limit=2)
    await memory.initialize()
    await memory.add_node("s1", "decision", "old decision")
    await memory.add_node("s1", "fact", "a fact")
    await memory.add_node("s1", "decision", "new decision")
    for index in range(3):
        await memory.add_node("s1", "user", f"chatter {index}")
    rows = await memory.fetch_context("s1", "")
    assert [row["content"] for row in rows] == ["a fact", "new decision", "chatter 2"]

    text = format_context(rows)
    assert text is not None
    assert text.index("Project decisions and facts:") < text.index("- [fact] a fact") < text.index("Related conversation:")


async def test_existing_database_is_indexed_on_upgrade(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    from middle_agentic.memory.graph import SCHEMA

    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO nodes (id, session_id, role, content, created_at) VALUES ('n1', 's1', 'note', 'kafka topic naming', 1)"
    )
    connection.commit()
    connection.close()

    memory = GraphMemory(db_path, recent_limit=0)
    await memory.initialize()
    assert memory.fts_enabled
    assert [row["id"] for row in await memory.fetch_context("s1", "kafka")] == ["n1"]
    await memory.initialize()  # idempotent: no duplicate index rows
    assert len(await memory.fetch_context("s1", "kafka")) == 1


async def test_like_fallback_without_fts(memory: GraphMemory) -> None:
    memory.fts_enabled = False
    await memory.add_node("s1", "user", "redis caching chatter")
    await memory.add_node("s1", "decision", "redis for caching")
    await memory.add_node("s1", "user", "more redis chatter")
    rows = await memory.fetch_context("s1", "redis", recent_limit=0, related_limit=1)
    assert [row["role"] for row in rows] == ["decision"]


async def test_forget_removes_nodes_edges_and_index(memory: GraphMemory) -> None:
    await memory.record_exchange("s1", "Tailscale ip?", "I cannot tell", {})
    await memory.record_exchange("other", "Tailscale ip?", "keep me", {})
    decision = await memory.add_node("s1", "decision", "Tailscale ACLs allow port 8000")

    assert len(await memory.forget("s1", match="ip?", dry_run=True)) == 2
    removed = await memory.forget("s1", match="ip?")
    assert [row["content"] for row in removed] == ["Tailscale ip?", "I cannot tell"]

    graph = await memory.session_graph("s1")
    assert [node["id"] for node in graph["nodes"]] == [decision] and graph["edges"] == []
    rows = await memory.fetch_context("s1", "tailscale", recent_limit=0)
    assert [row["id"] for row in rows] == [decision]
    assert len((await memory.session_graph("other"))["nodes"]) == 2

    with pytest.raises(ValueError):
        await memory.forget("s1")


def test_helpers() -> None:
    assert extract_keywords("What is the Postgres ledger setup?") == ["postgres", "ledger", "setup"]
    assert build_fts_query("veritabanında api_key") == '"veritabanı"* OR "api"* OR "key"*'
    assert build_fts_query("is it?") is None
    assert normalise_session_id("  proj ect/../x ") == "project..x"
    assert normalise_session_id(None) == "default"
    assert format_context([]) is None
    text = format_context([{"role": "user", "content": "a" * 20}], snippet_chars=5)
    assert text is not None and "- [user] aaaaa..." in text
