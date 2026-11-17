from __future__ import annotations

import sqlite3

import pytest

from middle_agentic.memory.graph import GraphMemory, extract_keywords, format_context, normalise_session_id


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


def test_helpers() -> None:
    assert extract_keywords("What is the Postgres ledger setup?") == ["postgres", "ledger", "setup"]
    assert normalise_session_id("  proj ect/../x ") == "project..x"
    assert normalise_session_id(None) == "default"
    assert format_context([]) is None
    text = format_context([{"role": "user", "content": "a" * 20}], snippet_chars=5)
    assert text is not None and "- [user] aaaaa..." in text
