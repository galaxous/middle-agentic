from __future__ import annotations

import pytest

from middle_agentic.config import TierConfig
from middle_agentic.providers.chain import AllTiersFailedError, ModelRouter, UnknownModelError
from middle_agentic.providers.registry import LOCAL_API_KEY, resolve_chain, resolve_tier

from conftest import rate_limit

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def router(settings) -> ModelRouter:
    return ModelRouter(resolve_chain(settings.chain))


def test_select_auto_name_and_label(router: ModelRouter) -> None:
    assert [t.name for t in router.select("auto")] == ["primary", "secondary", "paid"]
    assert [t.name for t in router.select(None)] == ["primary", "secondary", "paid"]
    assert [t.name for t in router.select("secondary")] == ["secondary"]
    assert [t.name for t in router.select("free")] == ["primary", "secondary"]
    with pytest.raises(UnknownModelError):
        router.select("gpt-4o")


async def test_falls_back_on_rate_limit(router: ModelRouter, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = rate_limit()
    response, tier, attempts = await router.complete(MESSAGES, {})
    assert tier.name == "secondary"
    assert response.choices[0].message.content == "answer from gemini/m-secondary"
    assert [a["status"] for a in attempts] == ["failed", "ok"]
    assert fake_llm.calls[0]["num_retries"] == 0


async def test_skips_tier_without_key(router: ModelRouter, fake_llm, monkeypatch) -> None:
    monkeypatch.delenv("TEST_KEY_A")
    _, tier, attempts = await router.complete(MESSAGES, {})
    assert tier.name == "secondary"
    assert attempts[0] == {"name": "primary", "model": "m-primary", "tier": "free", "status": "skipped", "reason": "TEST_KEY_A is not set"}
    assert fake_llm.models_called() == ["gemini/m-secondary"]


async def test_all_tiers_failed(router: ModelRouter, fake_llm) -> None:
    for model in ("openai/m-primary", "gemini/m-secondary", "openai/m-paid"):
        fake_llm.behaviour[model] = rate_limit()
    with pytest.raises(AllTiersFailedError) as error:
        await router.complete(MESSAGES, {})
    assert len(error.value.attempts) == 3


async def test_explicit_tier_does_not_fall_back(router: ModelRouter, fake_llm) -> None:
    fake_llm.behaviour["gemini/m-secondary"] = rate_limit()
    with pytest.raises(AllTiersFailedError):
        await router.complete(MESSAGES, {}, requested="secondary")
    assert fake_llm.models_called() == ["gemini/m-secondary"]


async def test_stream_falls_back_before_first_chunk(router: ModelRouter, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = [rate_limit()]
    fake_llm.behaviour["gemini/m-secondary"] = ["Hel", "lo"]
    result = await router.stream(MESSAGES, {})
    chunks = [chunk.text async for chunk in result.chunks]
    assert result.tier.name == "secondary"
    assert chunks == ["Hel", "lo"]


async def test_stream_error_after_first_chunk_is_not_retried(router: ModelRouter, fake_llm) -> None:
    fake_llm.behaviour["openai/m-primary"] = ["partial", rate_limit()]
    result = await router.stream(MESSAGES, {})
    assert result.tier.name == "primary"
    received = []
    with pytest.raises(Exception):
        async for chunk in result.chunks:
            received.append(chunk.text)
    assert received == ["partial"]
    assert fake_llm.models_called() == ["openai/m-primary"]


async def test_caller_params_cannot_override_routing(router: ModelRouter, fake_llm) -> None:
    await router.complete(MESSAGES, {"model": "evil", "api_base": "http://evil", "temperature": 0.2})
    call = fake_llm.calls[0]
    assert call["model"] == "openai/m-primary"
    assert call["temperature"] == 0.2
    assert call.get("api_base") != "http://evil"


def test_local_runtimes_never_receive_the_real_openai_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    tier = resolve_tier(TierConfig(name="cpp", tier="local", runtime="llamacpp", model="local"))
    kwargs = tier.call_kwargs()
    assert kwargs["model"] == "openai/local"
    assert kwargs["api_base"] == "http://127.0.0.1:8080/v1"
    assert kwargs["api_key"] == LOCAL_API_KEY


@pytest.mark.parametrize(
    ("runtime", "expected_prefix"),
    [("ollama", "ollama_chat/"), ("vllm", "hosted_vllm/"), ("lmstudio", "lm_studio/"), ("anthropic", "anthropic/"), ("openrouter", "openrouter/")],
)
def test_runtime_prefixes(runtime: str, expected_prefix: str) -> None:
    tier = resolve_tier(TierConfig(name="t", runtime=runtime, model="m"))
    assert tier.litellm_model == f"{expected_prefix}m"


def test_unknown_runtime_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown runtime"):
        resolve_tier(TierConfig(name="t", runtime="nope", model="m"))


async def test_ollama_tier_unavailable_when_model_not_pulled(monkeypatch) -> None:
    tier = resolve_tier(TierConfig(name="local", tier="local", runtime="ollama", model="qwen2.5:7b"))

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"models": [{"name": "qwen2.5:0.5b"}]}

    class Client:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url):
            assert url == "http://127.0.0.1:11434/api/tags"
            return Response()

    monkeypatch.setattr("middle_agentic.providers.registry.httpx.AsyncClient", Client)
    available, reason = await tier.availability()
    assert not available
    assert "not pulled" in reason
