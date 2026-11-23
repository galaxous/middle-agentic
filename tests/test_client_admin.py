"""Client-side v0.5.0 surface: job/budget headers and read-only admin calls.

These tests never reach a gateway: httpx is given a MockTransport, so they run offline and
do not depend on the gateway's /admin endpoints being deployed yet.
"""
from __future__ import annotations

import json

import httpx
import pytest

from middle_agentic.cli import main
from middle_agentic.client import sdk as sdk_module
from middle_agentic.client.sdk import MiddleAgentClient

URL = "http://gateway.test:8000"
_REAL_HTTP_CLIENT = httpx.Client


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route every MiddleAgentClient request to a fake gateway that records what it receives."""
    requests: list[httpx.Request] = []
    replies: dict[str, object] = {
        "/admin/usage": {"data": [{"name": "gemini-flash", "requests": 3, "prompt_tokens": 900,
                                   "completion_tokens": 120, "cost_usd": 0.0021}]},
        "/admin/budget": {"daily_usd": 1.0, "spent_usd": 0.42, "on_exceed": "downgrade"},
        "/admin/models": {"data": [{"name": "luna", "tier": "paid", "model": "gpt-6-luna", "enabled": True,
                                    "price_in": 0.1, "price_out": 0.5, "overridden": False}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/chat/completions":
            body = {"choices": [{"message": {"content": "ok"}}]}
            return httpx.Response(200, json=body, headers={"x-gateway-tier-name": "luna"})
        return httpx.Response(200, json=replies.get(request.url.path, {"ok": True}))

    def make_client(**kwargs):
        return _REAL_HTTP_CLIENT(transport=httpx.MockTransport(handler), **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(sdk_module.httpx, "Client", make_client)
    return requests


def test_chat_sends_job_and_budget_headers(captured: list[httpx.Request]) -> None:
    with MiddleAgentClient(base_url=URL, api_key="k", session_id="s") as client:
        client.ask("hello", job="daily-digest", budget_usd=0.25)
    headers = captured[-1].headers
    assert headers["x-job"] == "daily-digest"
    assert headers["x-budget-usd"] == "0.2500"
    assert headers["x-memory"] == "on"


def test_chat_omits_job_and_budget_when_not_given(captured: list[httpx.Request]) -> None:
    with MiddleAgentClient(base_url=URL, api_key="k", session_id="s") as client:
        client.ask("hello")
    assert "x-job" not in captured[-1].headers
    assert "x-budget-usd" not in captured[-1].headers


def test_negative_budget_is_rejected_before_sending(captured: list[httpx.Request]) -> None:
    with MiddleAgentClient(base_url=URL, api_key="k", session_id="s") as client:
        with pytest.raises(ValueError):
            client.ask("hello", budget_usd=-1)
    assert captured == []


def test_stream_sends_job_and_budget_headers(captured: list[httpx.Request]) -> None:
    def stream_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="data: [DONE]\n\n")

    transport = httpx.MockTransport(stream_handler)
    with MiddleAgentClient(base_url=URL, api_key="k", session_id="s") as client:
        client._http = _REAL_HTTP_CLIENT(base_url=URL, transport=transport)
        assert list(client.stream("hi", job="news", budget_usd=0.1)) == []
    assert captured[-1].headers["x-job"] == "news"
    assert captured[-1].headers["x-budget-usd"] == "0.1000"


def test_usage_budget_and_models_call_admin_endpoints(captured: list[httpx.Request]) -> None:
    with MiddleAgentClient(base_url=URL, api_key="k") as client:
        rows = client.usage(by="job", days=7)
        budget = client.budget()
        tiers = client.admin_models()
    assert rows[0]["name"] == "gemini-flash"
    assert budget["spent_usd"] == 0.42
    assert tiers[0]["name"] == "luna"
    usage_request = captured[0]
    assert usage_request.url.path == "/admin/usage"
    assert usage_request.url.params["by"] == "job"
    assert usage_request.url.params["days"] == "7"


def test_cli_ask_passes_job_and_budget(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["ask", "hello", "--node", URL, "--job", "digest", "--budget", "0.05", "--quiet"])
    assert code == 0
    assert captured[-1].headers["x-job"] == "digest"
    assert captured[-1].headers["x-budget-usd"] == "0.0500"
    assert capsys.readouterr().out.strip() == "ok"


def test_cli_usage_prints_table_and_json(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["usage", "--node", URL, "--by", "tier"]) == 0
    table = capsys.readouterr().out
    assert "gemini-flash" in table and "0.0021" in table

    assert main(["usage", "--node", URL, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["requests"] == 3


def test_cli_budget_shows_spend_against_limit(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["budget", "--node", URL]) == 0
    assert capsys.readouterr().out.strip() == "today: $0.4200 of $1.00 (on exceed: downgrade)"


def test_cli_models_lists_prices(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "--node", URL]) == 0
    out = capsys.readouterr().out
    assert "luna" in out and "0.1/0.5" in out and "gpt-6-luna" in out


def test_cli_admin_forbidden_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "scope 'admin' required"})

    monkeypatch.setattr(
        sdk_module.httpx,
        "Client",
        lambda **kw: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(forbidden), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    assert main(["budget", "--node", URL]) != 0
    assert "gateway returned 403" in capsys.readouterr().err


def test_live_changes_send_only_the_given_fields(captured: list[httpx.Request]) -> None:
    with MiddleAgentClient(base_url=URL, api_key="k") as client:
        client.patch_model("primary", enabled=False, model=None, price_in=0.5)
        client.set_model_order(["paid", "primary"])
        client.set_budget(daily_usd=0.5)
        client.clear_budget()
    patch, order, budget, clear = captured
    assert patch.method == "PATCH" and patch.url.path == "/admin/models/primary"
    assert json.loads(patch.content) == {"enabled": False, "price_in": 0.5}
    assert json.loads(order.content) == {"names": ["paid", "primary"]}
    assert budget.method == "PUT" and json.loads(budget.content) == {"daily_usd": 0.5}
    assert clear.method == "DELETE" and clear.url.path == "/admin/budget"


def test_cli_models_disable_and_reorder(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["models", "disable", "luna", "--node", URL]) == 0
    assert json.loads(captured[-1].content) == {"enabled": False}
    assert captured[-1].method == "PATCH" and captured[-1].url.path == "/admin/models/luna"

    assert main(["models", "reorder", "local", "luna", "--node", URL]) == 0
    assert json.loads(captured[-1].content) == {"names": ["local", "luna"]}


def test_cli_models_set_requires_a_change(captured: list[httpx.Request]) -> None:
    assert main(["models", "set", "luna", "--node", URL]) != 0
    assert all(request.method != "PATCH" for request in captured)


def test_cli_budget_set_and_reset(captured: list[httpx.Request], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["budget", "set", "--daily", "0.25", "--on-exceed", "block", "--node", URL]) == 0
    assert json.loads(captured[-1].content) == {"daily_usd": 0.25, "on_exceed": "block"}
    assert main(["budget", "reset", "--node", URL]) == 0
    assert captured[-1].method == "DELETE"
