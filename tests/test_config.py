from __future__ import annotations

import pytest
from pydantic import ValidationError

from middle_agentic import config
from middle_agentic.config import LLMConfig, Settings, TierConfig, default_chain, load_settings, parse_legacy_chain

ENV_VARS = (
    "MODEL_CHAIN", "GATEWAY_HOST", "GATEWAY_PORT", "GATEWAY_API_KEY", "MAGENT_ROLES", "MAGENT_NODE_NAME",
    "LLM_MODEL", "LLM_RUNTIME", "MEMORY_ENABLED", "MAGENT_CONFIG",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_environment", lambda: None)


def test_legacy_model_chain() -> None:
    tiers = parse_legacy_chain("free:gemini/gemini-2.5-flash, free:openrouter/openrouter/free, paid:openai/gpt-6-luna, ollama_chat/qwen2.5:0.5b, weird/x")
    assert [(t.tier, t.runtime, t.model) for t in tiers] == [
        ("free", "gemini", "gemini-2.5-flash"),
        ("free", "openrouter", "openrouter/free"),
        ("paid", "openai", "gpt-6-luna"),
        ("custom", "ollama", "qwen2.5:0.5b"),
        ("custom", "litellm", "weird/x"),
    ]
    assert tiers[0].name == "gemini/gemini-2.5-flash"


def test_legacy_chain_keeps_colons_in_model_ids() -> None:
    (tier,) = parse_legacy_chain("openrouter/meta-llama/llama-3.3-70b-instruct:free")
    assert tier.tier == "custom"
    assert tier.model == "meta-llama/llama-3.3-70b-instruct:free"


def test_default_chain_is_cloud_first_and_follows_llm_section() -> None:
    chain = default_chain(LLMConfig(runtime="ollama", model="qwen2.5:0.5b"))
    assert [(t.name, t.tier) for t in chain] == [("gemini-flash", "free"), ("openrouter-free", "free"), ("local", "local"), ("luna", "paid")]
    assert chain[2].model == "qwen2.5:0.5b"


def test_yaml_with_env_overrides(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "node: {name: acer, roles: [gateway, llm]}\n"
        "gateway: {host: 0.0.0.0, port: 9000}\n"
        "llm: {model: qwen2.5:7b}\n"
        "peers: [{name: pi, url: 'http://pi:8000/'}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GATEWAY_PORT", "9100")
    monkeypatch.setenv("GATEWAY_API_KEY", "k")
    settings = load_settings(path)
    assert settings.node.name == "acer"
    assert settings.node.roles == ["gateway", "llm"]
    assert settings.gateway.host == "0.0.0.0"
    assert settings.gateway.port == 9100
    assert settings.gateway.api_key == "k"
    assert next(t for t in settings.chain if t.name == "local").model == "qwen2.5:7b"
    assert settings.peers[0].name == "pi"


def test_model_chain_env_replaces_yaml_chain(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("chain: [{name: a, runtime: openai, model: x}]\n", encoding="utf-8")
    monkeypatch.setenv("MODEL_CHAIN", "paid:openai/gpt-6-luna")
    settings = load_settings(path)
    assert [t.name for t in settings.chain] == ["openai/gpt-6-luna"]


def test_roles_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MAGENT_ROLES", "client")
    assert load_settings(tmp_path / "missing.yaml").node.roles == ["client"]


def test_validation_errors() -> None:
    with pytest.raises(ValidationError, match="unknown roles"):
        Settings.model_validate({"node": {"roles": ["gateway", "router"]}})
    with pytest.raises(ValidationError, match="duplicate chain tier names"):
        Settings(chain=[TierConfig(name="a", runtime="openai", model="x"), TierConfig(name="a", runtime="gemini", model="y")])


def test_relative_db_path_resolves_to_project_root() -> None:
    settings = Settings()
    assert settings.memory.resolved_db_path() == config.PROJECT_ROOT / "agent_memory.db"
