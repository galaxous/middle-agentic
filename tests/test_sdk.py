from __future__ import annotations

import pytest

from middle_agentic.client.discovery import resolve_node
from middle_agentic.client.sdk import GatewayError, MiddleAgentClient
from middle_agentic.config import PeerConfig

from conftest import API_KEY, rate_limit, wait_for


@pytest.fixture
def sdk(live_server: str, settings) -> MiddleAgentClient:
    with MiddleAgentClient(base_url=live_server, api_key=API_KEY, session_id="sdk", settings=settings) as client:
        yield client


def test_chat_reports_serving_tier(sdk: MiddleAgentClient, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = rate_limit()
    result = sdk.ask("hello", system="be brief", temperature=0)
    assert result.text == "answer from gemini/m-secondary"
    assert (result.node, result.tier_name, result.tier, result.model) == ("test-node", "secondary", "free", "m-secondary")
    assert result.attempts == 2
    assert fake_llm.calls[-1]["messages"][0] == {"role": "system", "content": "be brief"}


def test_stream_yields_deltas(sdk: MiddleAgentClient, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = ["a", "b", "c"]
    assert "".join(sdk.stream("spell")) == "abc"
    assert sdk.last_headers["x-gateway-tier-name"] == "primary"


def test_memory_primitives(sdk: MiddleAgentClient) -> None:
    event = sdk.record_event("Release train leaves every Tuesday", role="fact")
    assert event["session_id"] == "sdk"
    context = sdk.context("when does the release train leave")
    assert "Release train leaves every Tuesday" in context["context"]
    assert sdk.session_graph()["nodes"][0]["id"] == event["id"]
    assert sdk.sessions()[0]["session_id"] == "sdk"


def test_chat_is_remembered(sdk: MiddleAgentClient) -> None:
    sdk.ask("The VPN gateway is wireguard-01")
    assert wait_for(lambda: len(sdk.session_graph()["nodes"]) == 2)


def test_hook_passthrough(sdk: MiddleAgentClient) -> None:
    payload = {"hook_event_name": "beforeSubmitPrompt", "prompt": "x", "workspace_roots": ["/tmp/w"]}
    assert sdk.hook("cursor", "beforeSubmitPrompt", payload) == {"continue": True}


def test_errors_are_raised_with_status(live_server: str, settings) -> None:
    with MiddleAgentClient(base_url=live_server, api_key="wrong", settings=settings) as client:
        with pytest.raises(GatewayError) as error:
            client.models()
    assert error.value.status_code == 401
    with MiddleAgentClient(base_url=live_server, api_key=API_KEY, settings=settings) as client:
        with pytest.raises(GatewayError) as error:
            client.ask("x", model="does-not-exist")
    assert error.value.status_code == 400


def test_resolve_node(settings, monkeypatch) -> None:
    monkeypatch.delenv("MAGENT_URL", raising=False)
    settings.peers = [PeerConfig(name="acer", url="http://acer-worker:8000/")]
    assert resolve_node(None, settings).url == "http://127.0.0.1:8000"
    assert resolve_node("local", settings).source == "local"
    assert resolve_node("ACER", settings).url == "http://acer-worker:8000"
    assert resolve_node("http://10.0.0.5:8000/", settings).url == "http://10.0.0.5:8000"
    assert resolve_node("pi", settings).url == "http://pi:8000"
    monkeypatch.setenv("MAGENT_URL", "http://hub:8000")
    assert resolve_node(None, settings).url == "http://hub:8000"
