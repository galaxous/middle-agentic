"""Request caps, cooldown after rate limits, capability checks, profiles and `models test`."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from middle_agentic.config import Settings
from middle_agentic.server.api_keys import ApiKeyStore
from middle_agentic.server.app import create_app

from conftest import API_KEY, FakeLLM, rate_limit

WORKER_KEY = "mag_worker_key_for_controls_tests_0123456789"


def bearer(key: str = API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "X-Memory": "off"}


def ask(client: TestClient, model: str = "auto", extra: dict | None = None):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **(extra or {})}
    return client.post("/v1/chat/completions", json=body, headers=bearer())


@pytest.fixture
def controls_settings(settings: Settings) -> Settings:
    ApiKeyStore(settings.memory.resolved_db_path()).create("worker", scopes="models,memory:read,memory:write", value=WORKER_KEY)
    return settings


def open_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_daily_request_cap_skips_the_tier_once_reached(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    controls_settings.chain[0] = controls_settings.chain[0].model_copy(update={"daily_request_cap": 1})
    with open_client(controls_settings) as client:
        assert ask(client).headers["x-gateway-tier-name"] == "primary"
        second = ask(client)
    assert second.headers["x-gateway-tier-name"] == "secondary"
    assert fake_llm.models_called().count("openai/m-primary") == 1


def test_rate_limit_puts_the_tier_in_cooldown(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    fake_llm.behaviour["openai/m-primary"] = rate_limit()
    with open_client(controls_settings) as client:
        assert ask(client).headers["x-gateway-tier-name"] == "secondary"
        assert ask(client).headers["x-gateway-tier-name"] == "secondary"
    # The second request never reached the rate-limited tier again.
    assert fake_llm.models_called().count("openai/m-primary") == 1


def test_tool_calls_skip_tiers_that_do_not_support_them(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    controls_settings.chain[0] = controls_settings.chain[0].model_copy(update={"supports_tools": False})
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}}]
    with open_client(controls_settings) as client:
        plain = ask(client)
        with_tools = ask(client, extra={"tools": tools})
    assert plain.headers["x-gateway-tier-name"] == "primary"
    assert with_tools.headers["x-gateway-tier-name"] == "secondary"


def test_prompt_larger_than_context_window_skips_the_tier(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    controls_settings.chain[0] = controls_settings.chain[0].model_copy(update={"context_window": 10})
    long_prompt = "word " * 500
    with open_client(controls_settings) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": long_prompt}]},
            headers=bearer(),
        )
    assert response.headers["x-gateway-tier-name"] == "secondary"


def test_cheap_first_profile_puts_unpriced_free_tiers_before_paid(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    controls_settings.chain[2] = controls_settings.chain[2].model_copy(update={"price_in": 1.0, "price_out": 1.0})
    with open_client(controls_settings) as client:
        router = client.app.state.gateway.router
        cheap = [t.name for t in router.select("profile:cheap-first")]
        quality = [t.name for t in router.select("profile:quality-first")]
        response = ask(client, "profile:cheap-first")
    assert cheap == ["primary", "secondary", "paid"]
    assert quality == ["paid", "primary", "secondary"]
    assert response.status_code == 200


def test_unknown_profile_is_refused(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    with open_client(controls_settings) as client:
        assert ask(client, "profile:fastest").status_code == 400


def test_models_test_answers_from_the_tier_and_records_it(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    with open_client(controls_settings) as client:
        result = client.post("/admin/models/primary/test", headers=bearer()).json()
        rows = client.get("/admin/usage", params={"by": "job"}, headers=bearer()).json()["data"]
    assert result["ok"] is True
    assert result["reply"] == "answer from openai/m-primary"
    assert any(row["name"] == "models-test" and row["requests"] == 1 for row in rows)


def test_models_test_reports_a_failing_tier_without_raising(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    fake_llm.behaviour["openai/m-primary"] = rate_limit()
    with open_client(controls_settings) as client:
        result = client.post("/admin/models/primary/test", headers=bearer()).json()
    assert result["ok"] is False and "RateLimitError" in result["error"]


def test_models_test_refuses_a_disabled_tier(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    with open_client(controls_settings) as client:
        client.patch("/admin/models/primary", json={"enabled": False}, headers=bearer())
        response = client.post("/admin/models/primary/test", headers=bearer())
    assert response.status_code == 409


def test_models_test_needs_the_admin_scope(controls_settings: Settings, fake_llm: FakeLLM) -> None:
    with open_client(controls_settings) as client:
        response = client.post("/admin/models/primary/test", headers={"Authorization": f"Bearer {WORKER_KEY}"})
    assert response.status_code == 403
