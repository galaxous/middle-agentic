from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import AUTH, rate_limit, wait_for


def chat(client: TestClient, content: str, session: str = "s1", **body) -> dict:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": content}], **body},
        headers={**AUTH, "X-Session-Id": session},
    )
    assert response.status_code == 200, response.text
    return response


def test_health_is_public_and_reports_chain(client: TestClient) -> None:
    payload = client.get("/health").json()
    assert payload["service"] == "middle-agentic"
    assert payload["node"] == {"name": "test-node", "roles": ["gateway", "client"]}
    assert payload["auth_required"] is True
    assert [tier["name"] for tier in payload["chain"]] == ["primary", "secondary", "paid"]


def test_auth_is_required(client: TestClient) -> None:
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401
    models = client.get("/v1/models", headers=AUTH).json()["data"]
    assert [m["id"] for m in models] == ["auto", "primary", "secondary", "paid"]


def test_chat_headers_and_memory_round_trip(client: TestClient, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = rate_limit()
    first = chat(client, "Remember: the staging database is called orchid")
    assert first.headers["x-gateway-tier-name"] == "secondary"
    assert first.headers["x-gateway-attempts"] == "2"
    assert first.json()["choices"][0]["message"]["content"] == "answer from gemini/m-secondary"

    chat(client, "What is the staging database called?")
    injected = fake_llm.calls[-1]["messages"]
    assert injected[0]["role"] == "system"
    assert "orchid" in injected[0]["content"]


def test_memory_can_be_switched_off_per_request(client: TestClient, fake_llm) -> None:
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "private"}]},
        headers={**AUTH, "X-Session-Id": "off", "X-Memory": "off"},
    )
    assert client.get("/v1/memory/sessions/off", headers=AUTH).json()["nodes"] == []


def test_system_prompt_stays_first(client: TestClient, fake_llm) -> None:
    chat(client, "note about kiwis")
    client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "kiwis?"}]},
        headers={**AUTH, "X-Session-Id": "s1"},
    )
    messages = fake_llm.calls[-1]["messages"]
    assert messages[0] == {"role": "system", "content": "be brief"}
    assert messages[1]["role"] == "system" and "kiwis" in messages[1]["content"]


def test_unknown_model_is_rejected(client: TestClient, fake_llm) -> None:
    response = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}]}, headers=AUTH)
    assert response.status_code == 400
    assert fake_llm.calls == []


def test_tier_label_selection(client: TestClient, fake_llm) -> None:
    response = chat(client, "x", model="paid")
    assert response.headers["x-gateway-tier-name"] == "paid"


def test_untrusted_params_are_dropped(client: TestClient, fake_llm) -> None:
    chat(client, "x", api_base="http://evil", api_key="stolen", temperature=0.1)
    call = fake_llm.calls[-1]
    assert call["temperature"] == 0.1
    assert call["api_key"] == "key-a"
    assert "api_base" not in call


def test_all_tiers_failed_returns_503(client: TestClient, fake_llm) -> None:
    for model in ("openai/m-primary", "gemini/m-secondary", "openai/m-paid"):
        fake_llm.behaviour[model] = rate_limit()
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "all_tiers_failed"


def test_streaming_sse_and_post_hook(client: TestClient, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = ["Hello", " world"]
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "greet me"}], "stream": True},
        headers={**AUTH, "X-Session-Id": "stream"},
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-gateway-tier-name"] == "primary"
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    deltas = [json.loads(line[6:])["choices"][0]["delta"]["content"] for line in lines[:-1]]
    assert deltas == ["Hello", " world"]

    def recorded() -> bool:
        nodes = client.get("/v1/memory/sessions/stream", headers=AUTH).json()["nodes"]
        return [n["content"] for n in nodes] == ["greet me", "Hello world"]

    assert wait_for(recorded)


def test_memory_api(client: TestClient) -> None:
    created = client.post(
        "/v1/memory/events",
        json={"session_id": "api", "role": "decision", "content": "Adopt trunk based development", "metadata": {"by": "ops"}},
        headers=AUTH,
    )
    assert created.status_code == 201
    node_id = created.json()["id"]

    linked = client.post(
        "/v1/memory/events",
        json={"session_id": "api", "role": "note", "content": "CI updated", "link_to": node_id, "relation": "implements"},
        headers=AUTH,
    )
    assert linked.status_code == 201
    assert client.post("/v1/memory/events", json={"session_id": "api", "content": "x", "link_to": "missing"}, headers=AUTH).status_code == 400
    assert client.post("/v1/memory/events", json={"content": "x", "role": "Bad Role"}, headers=AUTH).status_code == 422

    context = client.post("/v1/memory/context", json={"session_id": "api", "query": "trunk development"}, headers=AUTH).json()
    assert "Adopt trunk based development" in context["context"]

    graph = client.get("/v1/memory/sessions/api", headers=AUTH).json()
    assert {e["relation"] for e in graph["edges"]} == {"follows", "implements"}
    assert client.get("/v1/memory/sessions", headers=AUTH).json()["sessions"][0]["session_id"] == "api"
