from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import litellm
import pytest
import uvicorn
from fastapi.testclient import TestClient

from middle_agentic.config import DiscoveryConfig, GatewayConfig, MemoryConfig, NodeConfig, Settings, TierConfig
from middle_agentic.providers import chain as chain_module
from middle_agentic.server.api_keys import SCOPES, ApiKeyStore
from middle_agentic.server.app import create_app

API_KEY = "mag_test_key_for_tests_only_0123456789"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


class FakeChunk:
    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": "chunk",
            "object": "chat.completion.chunk",
            "model": "fake",
            "choices": [{"index": 0, "delta": {"content": self.text}}],
        }


class FakeLLM:
    """Stand-in for litellm.acompletion.

    ``behaviour`` maps a LiteLLM model string to: an exception (raised), a string (answer) or a
    list of strings/exceptions (stream chunks; an exception item breaks the stream there).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.behaviour: dict[str, Any] = {}

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        behaviour = self.behaviour.get(kwargs["model"], f"answer from {kwargs['model']}")
        if isinstance(behaviour, BaseException):
            raise behaviour
        if kwargs.get("stream"):
            parts = behaviour if isinstance(behaviour, list) else [behaviour]

            async def generate():
                for part in parts:
                    if isinstance(part, BaseException):
                        raise part
                    yield FakeChunk(part)

            return generate()
        text = behaviour if isinstance(behaviour, str) else "".join(p for p in behaviour if isinstance(p, str))
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        )

    def models_called(self) -> list[str]:
        return [call["model"] for call in self.calls]


def rate_limit(model: str = "x") -> litellm.RateLimitError:
    return litellm.RateLimitError(message="rate limited", llm_provider="openai", model=model)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(chain_module.litellm, "acompletion", fake)
    return fake


class _EmptyStore:
    def get(self, provider):
        return None


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider keys come from the environment in tests; the machine's Keychain/DPAPI is never read."""
    from middle_agentic import tokens

    monkeypatch.setattr(tokens, "_secret_store", lambda: _EmptyStore())
    tokens.clear_secret_cache()


@pytest.fixture(autouse=True)
def isolated_magent_home(tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own magent home, so no real .env, config or database is read."""
    monkeypatch.setenv("MAGENT_HOME", str(tmp_path_factory.mktemp("magent-home")))


@pytest.fixture
def settings(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TEST_KEY_A", "key-a")
    monkeypatch.setenv("TEST_KEY_B", "key-b")
    settings = Settings(
        node=NodeConfig(name="test-node", roles=["gateway", "client"]),
        gateway=GatewayConfig(),
        memory=MemoryConfig(db_path=str(tmp_path / "memory.db")),
        chain=[
            TierConfig(name="primary", tier="free", runtime="openai", model="m-primary", api_key_env="TEST_KEY_A"),
            TierConfig(name="secondary", tier="free", runtime="gemini", model="m-secondary", api_key_env="TEST_KEY_B"),
            TierConfig(name="paid", tier="paid", runtime="openai", model="m-paid", api_key_env="TEST_KEY_A"),
        ],
        discovery=DiscoveryConfig(tailscale=False),
    )
    # The gateway only accepts keys from its own store; the test key goes there like `magent key create`.
    ApiKeyStore(settings.memory.resolved_db_path()).create("test", scopes=SCOPES, value=API_KEY)
    return settings


@pytest.fixture
def client(settings: Settings, fake_llm: FakeLLM) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def live_server(settings: Settings, fake_llm: FakeLLM) -> Iterator[str]:
    """A real uvicorn gateway on a free port (same process, so the fake LLM applies)."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False
