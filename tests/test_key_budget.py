"""Per-key daily budgets: optional, off by default, and limited to the key that has one."""
from __future__ import annotations

from fastapi.testclient import TestClient

from middle_agentic.config import Settings
from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.server.app import create_app

from conftest import API_KEY, FakeLLM

BUDGETED_KEY = "mag_budgeted_key_for_tests_only_0123456789"
ADMIN_KEY = "mag_admin_for_budget_tests_only_0123456789"
PAID_CALL_USD = 0.0015


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Memory": "off"}


def ask(client: TestClient, key: str, model: str = "auto"):
    return client.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                       headers=bearer(key))


def setup(settings: Settings, budget_usd: float | None = None, on_exceed: str | None = None) -> Settings:
    settings.chain[2] = settings.chain[2].model_copy(update={"price_in": 1.0, "price_out": 1.0})
    store = ApiKeyStore(settings.memory.resolved_db_path())
    store.create("budgeted", scopes="models,memory:read,memory:write", value=BUDGETED_KEY,
                 budget_usd=budget_usd, on_exceed=on_exceed)
    store.create("admin-test", scopes="admin,models,memory:read,memory:write", value=ADMIN_KEY)
    return settings


def test_keys_without_a_budget_keep_the_gateway_wide_behaviour(settings: Settings, fake_llm: FakeLLM) -> None:
    setup(settings)
    with TestClient(create_app(settings)) as client:
        assert ask(client, API_KEY, "paid").status_code == 200
        response = ask(client, API_KEY)
    assert response.headers["x-gateway-budget"] == "ok"


def test_a_key_budget_downgrades_only_that_key(settings: Settings, fake_llm: FakeLLM) -> None:
    setup(settings, budget_usd=0.001)
    with TestClient(create_app(settings)) as client:
        assert ask(client, BUDGETED_KEY, "paid").status_code == 200  # spends 0.0015 against its own 0.001
        limited = ask(client, BUDGETED_KEY)
        other = ask(client, API_KEY)
    assert limited.headers["x-gateway-budget"] == "downgraded"
    assert limited.headers["x-gateway-tier-name"] == "primary"
    assert other.headers["x-gateway-budget"] == "ok"


def test_a_key_budget_can_block_while_the_gateway_downgrades(settings: Settings, fake_llm: FakeLLM) -> None:
    setup(settings, budget_usd=0.001, on_exceed="block")
    with TestClient(create_app(settings)) as client:
        assert ask(client, BUDGETED_KEY, "paid").status_code == 200
        refused = ask(client, BUDGETED_KEY)
        allowed = ask(client, API_KEY)
    assert refused.status_code == 429
    assert refused.json()["error"]["budget_usd"] == 0.001
    assert allowed.status_code == 200


def test_admin_sets_and_clears_a_key_budget_live(settings: Settings, fake_llm: FakeLLM) -> None:
    setup(settings)
    with TestClient(create_app(settings)) as client:
        assert ask(client, BUDGETED_KEY, "paid").status_code == 200
        assert ask(client, BUDGETED_KEY).headers["x-gateway-budget"] == "ok"  # no key budget yet

        set_response = client.put("/admin/keys/budgeted/budget", json={"daily_usd": 0.001}, headers=bearer(ADMIN_KEY))
        assert set_response.status_code == 200 and set_response.json()["key_budget_usd"] == 0.001
        assert ask(client, BUDGETED_KEY).headers["x-gateway-budget"] == "downgraded"

        report = client.get("/admin/budget", params={"key": "budgeted"}, headers=bearer(ADMIN_KEY)).json()
        assert report["effective_daily_usd"] == 0.001
        assert report["spent_usd"] > 0

        cleared = client.put("/admin/keys/budgeted/budget", json={"clear": True}, headers=bearer(ADMIN_KEY)).json()
        assert cleared["key_budget_usd"] is None
        assert ask(client, BUDGETED_KEY).headers["x-gateway-budget"] == "ok"


def test_key_budget_inputs_are_validated(settings: Settings, fake_llm: FakeLLM) -> None:
    setup(settings)
    with TestClient(create_app(settings)) as client:
        zero = client.put("/admin/keys/budgeted/budget", json={"daily_usd": 0}, headers=bearer(ADMIN_KEY))
        bad_mode = client.put("/admin/keys/budgeted/budget", json={"on_exceed": "maybe"}, headers=bearer(ADMIN_KEY))
        missing = client.put("/admin/keys/nobody/budget", json={"daily_usd": 1}, headers=bearer(ADMIN_KEY))
        worker_denied = client.put("/admin/keys/budgeted/budget", json={"daily_usd": 1}, headers=bearer(BUDGETED_KEY))
    assert zero.status_code == 422
    assert bad_mode.status_code == 422
    assert missing.status_code == 404
    assert worker_denied.status_code == 403


def test_store_rejects_invalid_key_budgets(tmp_path) -> None:
    import pytest
    from middle_agentic.server.api_keys import ApiKeyError

    store = ApiKeyStore(tmp_path / "k.db")
    with pytest.raises(ApiKeyError):
        store.create("a", budget_usd=-1)
    with pytest.raises(ApiKeyError):
        store.create("b", on_exceed="sometimes")
    store.create("c", budget_usd=2.5, on_exceed="block")
    assert store.budget_for("c") == (2.5, "block")
    assert store.set_budget("c", clear=True) is True
    assert store.budget_for("c") == (None, None)
