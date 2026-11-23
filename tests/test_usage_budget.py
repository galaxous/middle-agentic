"""Usage ledger, daily budget and admin endpoints, exercised through the real app with a fake LLM."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from middle_agentic.config import Settings
from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.server.app import create_app

from conftest import API_KEY, FakeLLM, rate_limit

ADMIN_KEY = "mag_admin_key_for_tests_only_0123456789"
WORKER_KEY = "mag_worker_key_for_tests_only_0123456789"
PAID_CALL_USD = 0.0015  # 1000 prompt + 500 completion tokens at 1.0 USD per 1M on both sides


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Memory": "off"}


@pytest.fixture
def priced(settings: Settings) -> Settings:
    """The paid tier has explicit prices, so its cost is exact and known."""
    settings.chain[2] = settings.chain[2].model_copy(update={"price_in": 1.0, "price_out": 1.0})
    store = ApiKeyStore(settings.memory.resolved_db_path())
    store.create("admin-test", scopes="admin,models,memory:read,memory:write", value=ADMIN_KEY)
    store.create("worker", scopes="models,memory:read,memory:write", value=WORKER_KEY)
    return settings


def open_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def ask(client: TestClient, model: str, key: str = API_KEY, headers: dict[str, str] | None = None):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
        headers={**bearer(key), **(headers or {})},
    )


def test_completion_is_recorded_with_job_and_cost(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        response = ask(client, "paid", headers={"X-Job": "digest"})
        assert response.status_code == 200
        assert response.headers["x-gateway-budget"] == "ok"
        rows = client.get("/admin/usage", params={"by": "job"}, headers=bearer(ADMIN_KEY)).json()["data"]
    assert rows[0]["name"] == "digest"
    assert rows[0]["requests"] == 1
    assert rows[0]["cost_usd"] == pytest.approx(PAID_CALL_USD)


def test_budget_downgrades_to_free_tiers_once_spent(priced: Settings, fake_llm: FakeLLM) -> None:
    priced.budget.daily_usd = 0.001  # one paid call (0.0015) is already over this
    with open_client(priced) as client:
        assert ask(client, "paid").status_code == 200
        downgraded = ask(client, "auto")
        assert downgraded.status_code == 200
        assert downgraded.headers["x-gateway-budget"] == "downgraded"
        assert downgraded.headers["x-gateway-tier-name"] == "primary"
        assert fake_llm.calls[-1]["model"] == "openai/m-primary"

        blocked_paid = ask(client, "paid")
    assert blocked_paid.status_code == 429
    assert blocked_paid.json()["error"]["type"] == "budget_exceeded"


def test_block_mode_refuses_every_request_after_the_cap(priced: Settings, fake_llm: FakeLLM) -> None:
    priced.budget.daily_usd = 0.001
    priced.budget.on_exceed = "block"
    with open_client(priced) as client:
        assert ask(client, "paid").status_code == 200
        calls_before = len(fake_llm.calls)
        refused = ask(client, "auto")
    assert refused.status_code == 429
    assert refused.headers["x-gateway-budget"] == "blocked"
    assert len(fake_llm.calls) == calls_before  # no provider was contacted


def test_client_header_can_lower_the_budget(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        assert ask(client, "paid").status_code == 200  # spends 0.0015 of the 1.0 default
        assert ask(client, "auto").headers["x-gateway-budget"] == "ok"
        lowered = ask(client, "auto", headers={"X-Budget-USD": "0.001"})
    assert lowered.headers["x-gateway-budget"] == "downgraded"


def test_client_header_cannot_raise_the_budget(priced: Settings, fake_llm: FakeLLM) -> None:
    priced.budget.daily_usd = 0.001
    with open_client(priced) as client:
        assert ask(client, "paid").status_code == 200
        raised = ask(client, "auto", headers={"X-Budget-USD": "100"})
    assert raised.headers["x-gateway-budget"] == "downgraded"


@pytest.mark.parametrize("value", ["abc", "-1", "nan"])
def test_invalid_budget_header_is_rejected(priced: Settings, fake_llm: FakeLLM, value: str) -> None:
    with open_client(priced) as client:
        response = ask(client, "auto", headers={"X-Budget-USD": value})
    assert response.status_code == 400


def test_streamed_completion_is_recorded(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "paid", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={**bearer(API_KEY), "X-Job": "stream-job"},
        ) as response:
            assert response.status_code == 200
            response.read()
        rows = client.get("/admin/usage", params={"by": "job"}, headers=bearer(ADMIN_KEY)).json()["data"]
    row = next(row for row in rows if row["name"] == "stream-job")
    assert row["requests"] == 1
    assert row["cost_usd"] > 0  # no usage chunk from the fake provider, so tokens are estimated


def test_failed_attempts_are_counted_without_cost(priced: Settings, fake_llm: FakeLLM) -> None:
    fake_llm.behaviour["openai/m-paid"] = rate_limit()
    with open_client(priced) as client:
        assert ask(client, "paid").status_code == 503
        rows = client.get("/admin/usage", params={"by": "tier"}, headers=bearer(ADMIN_KEY)).json()["data"]
    row = next(row for row in rows if row["name"] == "paid")
    assert row["requests"] == 1
    assert row["cost_usd"] == 0


def test_admin_endpoints_need_the_admin_scope(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        worker = client.get("/admin/budget", headers=bearer(WORKER_KEY))
        admin = client.get("/admin/budget", headers=bearer(ADMIN_KEY))
    assert worker.status_code == 403
    assert admin.status_code == 200
    assert admin.json()["daily_usd"] == 1.0
    assert admin.json()["on_exceed"] == "downgrade"


def test_admin_models_reports_prices(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        tiers = client.get("/admin/models", headers=bearer(ADMIN_KEY)).json()["data"]
    paid = next(tier for tier in tiers if tier["name"] == "paid")
    assert paid["price_in"] == 1.0
    assert paid["price_out"] == 1.0
    assert paid["enabled"] is True
    assert paid["overridden"] is False


def test_admin_usage_rejects_unknown_grouping(priced: Settings, fake_llm: FakeLLM) -> None:
    with open_client(priced) as client:
        response = client.get("/admin/usage", params={"by": "color"}, headers=bearer(ADMIN_KEY))
    assert response.status_code == 422
