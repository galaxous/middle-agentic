"""Live chain/budget changes through the admin API, and per-tier concurrency limits.

All changes are made while the app keeps running; the next request must see them.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from middle_agentic.config import Settings
from middle_agentic.providers.limits import ConcurrencyLimiter
from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.server.app import create_app

from conftest import API_KEY, FakeLLM

WORKER_KEY = "mag_worker_key_for_live_tests_0123456789"


def bearer(key: str = API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Memory": "off"}


@pytest.fixture
def app_settings(settings: Settings) -> Settings:
    settings.chain[2] = settings.chain[2].model_copy(update={"price_in": 1.0, "price_out": 1.0})
    ApiKeyStore(settings.memory.resolved_db_path()).create("worker", scopes="models,memory:read,memory:write", value=WORKER_KEY)
    return settings


@pytest.fixture
def live(app_settings: Settings, fake_llm: FakeLLM):
    with TestClient(create_app(app_settings)) as client:
        yield client


def ask(client: TestClient, model: str = "auto", key: str = API_KEY):
    return client.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}]}, headers=bearer(key))


def test_disabling_a_tier_takes_effect_on_the_next_request(live: TestClient, fake_llm: FakeLLM) -> None:
    assert ask(live).headers["x-gateway-tier-name"] == "primary"
    assert live.patch("/admin/models/primary", json={"enabled": False}, headers=bearer()).status_code == 200
    assert ask(live).headers["x-gateway-tier-name"] == "secondary"
    assert live.delete("/admin/models/primary", headers=bearer()).json()["reset"] is True
    assert ask(live).headers["x-gateway-tier-name"] == "primary"


def test_changing_a_model_takes_effect_immediately(live: TestClient, fake_llm: FakeLLM) -> None:
    live.patch("/admin/models/primary", json={"model": "m-swapped"}, headers=bearer())
    ask(live)
    assert fake_llm.calls[-1]["model"] == "openai/m-swapped"


def test_reordering_changes_the_chain_and_resets_cleanly(live: TestClient, fake_llm: FakeLLM) -> None:
    response = live.put("/admin/models/order", json={"names": ["paid", "secondary"]}, headers=bearer())
    assert response.status_code == 200
    assert ask(live).headers["x-gateway-tier-name"] == "paid"
    live.delete("/admin/models/order", headers=bearer())
    assert ask(live).headers["x-gateway-tier-name"] == "primary"


def test_runtime_budget_overrides_config_and_can_be_reset(live: TestClient, fake_llm: FakeLLM) -> None:
    assert ask(live, "paid").status_code == 200  # spends 0.0015 against the 1.0 default
    assert live.put("/admin/budget", json={"daily_usd": 0.001}, headers=bearer()).status_code == 200
    downgraded = ask(live)
    assert downgraded.headers["x-gateway-budget"] == "downgraded"

    live.put("/admin/budget", json={"on_exceed": "block"}, headers=bearer())
    blocked = ask(live)
    assert blocked.status_code == 429 and blocked.json()["error"]["on_exceed"] == "block"

    reset = live.delete("/admin/budget", headers=bearer()).json()
    assert reset["daily_usd"] == 1.0 and reset["source"] == "config"
    assert ask(live).headers["x-gateway-budget"] == "ok"


def test_models_report_shows_overrides_and_disabled_tiers(live: TestClient, fake_llm: FakeLLM) -> None:
    live.patch("/admin/models/paid", json={"enabled": False, "price_in": 1.5}, headers=bearer())
    rows = {row["name"]: row for row in live.get("/admin/models", headers=bearer()).json()["data"]}
    assert rows["paid"]["enabled"] is False and rows["paid"]["reason"] == "disabled"
    assert rows["paid"]["overridden"] is True and rows["paid"]["price_in"] == 1.5
    assert rows["primary"]["overridden"] is False and rows["primary"]["enabled"] is True


def test_worker_key_cannot_change_the_chain(live: TestClient) -> None:
    assert live.patch("/admin/models/primary", json={"enabled": False}, headers=bearer(WORKER_KEY)).status_code == 403
    assert live.put("/admin/budget", json={"daily_usd": 0}, headers=bearer(WORKER_KEY)).status_code == 403


def test_invalid_changes_are_refused(live: TestClient) -> None:
    assert live.patch("/admin/models/nope", json={"enabled": False}, headers=bearer()).status_code == 404
    assert live.patch("/admin/models/primary", json={"timeout": -1}, headers=bearer()).status_code == 422
    assert live.patch("/admin/models/primary", json={"unknown_field": 1}, headers=bearer()).status_code == 422
    assert live.put("/admin/budget", json={"on_exceed": "maybe"}, headers=bearer()).status_code == 422
    assert live.put("/admin/models/order", json={"names": ["primary", "primary"]}, headers=bearer()).status_code == 400
    assert live.put("/admin/models/order", json={"names": ["ghost"]}, headers=bearer()).status_code == 404


def test_saturated_tier_is_skipped_after_its_queue_wait(live: TestClient, fake_llm: FakeLLM) -> None:
    live.patch("/admin/models/primary", json={"max_concurrency": 1, "queue_wait": 0.05}, headers=bearer())
    state = live.app.state.gateway
    tier = next(t for t in state.router.current_tiers() if t.name == "primary")
    limiter = state.router._limiter(tier)
    limiter.in_use = limiter.limit  # another request holds the only slot

    response = ask(live)
    assert response.headers["x-gateway-tier-name"] == "secondary"
    limiter.in_use = 0
    assert ask(live).headers["x-gateway-tier-name"] == "primary"


def test_stream_keeps_its_slot_until_it_finishes(live: TestClient, fake_llm: FakeLLM) -> None:
    live.patch("/admin/models/primary", json={"max_concurrency": 1}, headers=bearer())
    state = live.app.state.gateway
    with live.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "primary", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=bearer(),
    ) as response:
        assert response.status_code == 200
        assert state.router.in_flight("primary") == 1
        response.read()
    assert state.router.in_flight("primary") == 0


def test_local_tiers_default_to_one_request_at_a_time() -> None:
    from middle_agentic.config import TierConfig
    from middle_agentic.providers.registry import resolve_tier

    local = resolve_tier(TierConfig(name="local", tier="local", runtime="ollama", model="m"))
    cloud = resolve_tier(TierConfig(name="cloud", tier="free", runtime="gemini", model="m"))
    assert local.max_concurrency == 1
    assert cloud.max_concurrency is None


@pytest.mark.asyncio
async def test_limiter_waits_for_a_slot_then_gives_up() -> None:
    limiter = ConcurrencyLimiter(limit=1)
    assert await limiter.acquire(0.0) is True
    assert await limiter.acquire(0.05) is False
    limiter.release()
    assert await limiter.acquire(0.0) is True


@pytest.mark.asyncio
async def test_limiter_resize_is_seen_by_waiters() -> None:
    limiter = ConcurrencyLimiter(limit=1)
    assert await limiter.acquire(0.0) is True
    waiter = asyncio.create_task(limiter.acquire(1.0))
    await asyncio.sleep(0.05)
    limiter.resize(2)
    assert await waiter is True


@pytest.mark.asyncio
async def test_unlimited_limiter_never_blocks() -> None:
    limiter = ConcurrencyLimiter(limit=None)
    for _ in range(50):
        assert await limiter.acquire(0.0) is True
