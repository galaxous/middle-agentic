from __future__ import annotations

from fastapi.testclient import TestClient

from middle_agentic.hooks.common import project_session_id

from conftest import AUTH

WORKSPACE = "/Users/dev/projects/shop"
SESSION = project_session_id(WORKSPACE)


def cursor_payload(event: str, **extra) -> dict:
    return {
        "hook_event_name": event,
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "model": "claude",
        "workspace_roots": [WORKSPACE],
        **extra,
    }


def claude_payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": "abc", "cwd": WORKSPACE, **extra}


def session_nodes(client: TestClient) -> list[dict]:
    return client.get(f"/v1/memory/sessions/{SESSION}", headers=AUTH).json()["nodes"]


def test_project_session_id_is_stable_and_cross_platform() -> None:
    assert project_session_id("/a/b/shop") == project_session_id("/a/b/shop/")
    assert project_session_id(r"C:\Users\dev\shop").startswith("project:shop-")
    assert project_session_id(None) == "default"


def test_cursor_records_prompt_and_answer(client: TestClient) -> None:
    response = client.post("/v1/hooks/cursor/beforeSubmitPrompt", json=cursor_payload("beforeSubmitPrompt", prompt="Add a cart page"), headers=AUTH)
    assert response.json() == {"continue": True}
    response = client.post("/v1/hooks/cursor/afterAgentResponse", json=cursor_payload("afterAgentResponse", text="Cart page added"), headers=AUTH)
    assert response.json() == {}

    nodes = session_nodes(client)
    assert [(n["role"], n["content"]) for n in nodes] == [("user", "Add a cart page"), ("assistant", "Cart page added")]
    assert nodes[0]["metadata"]["source"] == "cursor"
    assert nodes[0]["metadata"]["conversation_id"] == "conv-1"


def test_cursor_session_start_injects_project_memory(client: TestClient) -> None:
    assert client.post("/v1/hooks/cursor/sessionStart", json=cursor_payload("sessionStart"), headers=AUTH).json() == {}
    client.post("/v1/hooks/cursor/beforeSubmitPrompt", json=cursor_payload("beforeSubmitPrompt", prompt="We use Stripe for payments"), headers=AUTH)

    answer = client.post("/v1/hooks/cursor/sessionStart", json=cursor_payload("sessionStart"), headers=AUTH).json()
    assert "We use Stripe for payments" in answer["additional_context"]


def test_cursor_unknown_event_is_acknowledged(client: TestClient) -> None:
    assert client.post("/v1/hooks/cursor/afterFileEdit", json=cursor_payload("afterFileEdit"), headers=AUTH).json() == {}


def test_claude_prompt_gets_context_and_is_recorded(client: TestClient) -> None:
    client.post("/v1/memory/events", json={"session_id": SESSION, "role": "decision", "content": "Checkout uses Stripe webhooks"}, headers=AUTH)

    answer = client.post(
        "/v1/hooks/claude/UserPromptSubmit",
        json=claude_payload("UserPromptSubmit", prompt="How does checkout confirm Stripe payments?"),
        headers=AUTH,
    ).json()
    output = answer["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "Checkout uses Stripe webhooks" in output["additionalContext"]
    assert "How does checkout confirm" not in output["additionalContext"]

    client.post("/v1/hooks/claude/Stop", json=claude_payload("Stop", last_assistant_message="It verifies the webhook signature."), headers=AUTH)
    roles = [n["role"] for n in session_nodes(client)]
    assert roles == ["decision", "user", "assistant"]


def test_claude_session_start_without_memory_returns_empty(client: TestClient) -> None:
    assert client.post("/v1/hooks/claude/SessionStart", json=claude_payload("SessionStart"), headers=AUTH).json() == {}


def codex_payload(event: str, **extra) -> dict:
    return {"hook_event_name": event, "session_id": "codex-1", "turn_id": "t-1", "cwd": WORKSPACE, "model": "gpt", **extra}


def test_codex_prompt_gets_context_and_stop_records_answer(client: TestClient) -> None:
    client.post("/v1/memory/events", json={"session_id": SESSION, "role": "decision", "content": "Cart uses Redis sessions"}, headers=AUTH)

    answer = client.post(
        "/v1/hooks/codex/UserPromptSubmit", json=codex_payload("UserPromptSubmit", prompt="Where do carts live?"), headers=AUTH
    ).json()
    output = answer["hookSpecificOutput"]
    assert output["hookEventName"] == "UserPromptSubmit"
    assert "Cart uses Redis sessions" in output["additionalContext"]
    assert "Where do carts live" not in output["additionalContext"]

    client.post("/v1/hooks/codex/Stop", json=codex_payload("Stop", last_assistant_message="Carts are in Redis."), headers=AUTH)
    nodes = session_nodes(client)
    assert [n["role"] for n in nodes] == ["decision", "user", "assistant"]
    assert nodes[1]["metadata"]["source"] == "codex"


def test_codex_session_start_injects_recent_memory(client: TestClient) -> None:
    assert client.post("/v1/hooks/codex/SessionStart", json=codex_payload("SessionStart"), headers=AUTH).json() == {}
    client.post("/v1/hooks/codex/UserPromptSubmit", json=codex_payload("UserPromptSubmit", prompt="Use pnpm"), headers=AUTH)
    answer = client.post("/v1/hooks/codex/SessionStart", json=codex_payload("SessionStart"), headers=AUTH).json()
    assert "Use pnpm" in answer["hookSpecificOutput"]["additionalContext"]


def antigravity_payload(**extra) -> dict:
    return {
        "conversationId": "ag-conv-1",
        "workspacePaths": [WORKSPACE],
        "modelName": "gemini-3.6-flash-medium",
        **extra,
    }


def test_antigravity_pre_invocation_records_once_per_turn(client: TestClient) -> None:
    client.post("/v1/memory/events", json={"session_id": SESSION, "role": "fact", "content": "Orders ship from Berlin"}, headers=AUTH)

    first = client.post(
        "/v1/hooks/antigravity/PreInvocation",
        json=antigravity_payload(invocationNum=0, lastUserInput="Where do orders ship from?"),
        headers=AUTH,
    ).json()
    step = first["injectSteps"][0]["ephemeralMessage"]
    assert "Orders ship from Berlin" in step and "Where do orders ship from" not in step

    # A later invocation of the same turn (tool loop) must not record the prompt again.
    assert client.post(
        "/v1/hooks/antigravity/PreInvocation",
        json=antigravity_payload(invocationNum=1, lastUserInput="Where do orders ship from?"),
        headers=AUTH,
    ).json() == {}
    roles = [n["role"] for n in session_nodes(client)]
    assert roles.count("user") == 1


def test_antigravity_session_start_injects_system_message(client: TestClient) -> None:
    assert client.post("/v1/hooks/antigravity/SessionStart", json=antigravity_payload(), headers=AUTH).json() == {}
    client.post("/v1/memory/events", json={"session_id": SESSION, "role": "decision", "content": "Use Postgres 16"}, headers=AUTH)
    answer = client.post("/v1/hooks/antigravity/SessionStart", json=antigravity_payload(), headers=AUTH).json()
    step = answer["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "Use Postgres 16" in step


def test_antigravity_other_events_are_neutral(client: TestClient) -> None:
    for event in ("PostInvocation", "PostToolUse", "Stop"):
        assert client.post(f"/v1/hooks/antigravity/{event}", json=antigravity_payload(), headers=AUTH).json() == {}


def test_unknown_tool_and_auth(client: TestClient) -> None:
    assert client.post("/v1/hooks/vscode/x", json={}, headers=AUTH).status_code == 404
    assert client.post("/v1/hooks/cursor/beforeSubmitPrompt", json={}).status_code == 401


def test_hooks_are_neutral_when_memory_disabled(settings, fake_llm) -> None:
    from middle_agentic.server.app import create_app

    settings.memory.enabled = False
    with TestClient(create_app(settings)) as client:
        answer = client.post("/v1/hooks/cursor/beforeSubmitPrompt", json=cursor_payload("beforeSubmitPrompt", prompt="x"), headers=AUTH)
        assert answer.json() == {"continue": True}
        assert client.post("/v1/memory/context", json={}, headers=AUTH).status_code == 503
