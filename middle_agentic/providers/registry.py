"""Runtime registry: maps a configured tier (runtime + model) onto a LiteLLM call.

Local runtimes (ollama, llamacpp, vllm, lmstudio) are probed over HTTP to decide availability;
cloud runtimes are available when their API key environment variable is set.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from middle_agentic.config import TierConfig
from middle_agentic.tokens import provider_secret


@dataclass(frozen=True)
class RuntimeSpec:
    prefix: str
    key_env: str | None = None
    local: bool = False
    default_api_base: str | None = None
    health_path: str | None = None


RUNTIMES: dict[str, RuntimeSpec] = {
    # Local model servers
    "ollama": RuntimeSpec("ollama_chat/", local=True, default_api_base="http://127.0.0.1:11434", health_path="/api/tags"),
    "llamacpp": RuntimeSpec("openai/", local=True, default_api_base="http://127.0.0.1:8080/v1", health_path="/models"),
    "vllm": RuntimeSpec("hosted_vllm/", local=True, default_api_base="http://127.0.0.1:8001/v1", health_path="/models"),
    "lmstudio": RuntimeSpec("lm_studio/", local=True, default_api_base="http://127.0.0.1:1234/v1", health_path="/models"),
    # Cloud providers
    "openai": RuntimeSpec("openai/", key_env="OPENAI_API_KEY"),
    "anthropic": RuntimeSpec("anthropic/", key_env="ANTHROPIC_API_KEY"),
    "gemini": RuntimeSpec("gemini/", key_env="GEMINI_API_KEY"),
    "openrouter": RuntimeSpec("openrouter/", key_env="OPENROUTER_API_KEY"),
    "groq": RuntimeSpec("groq/", key_env="GROQ_API_KEY"),
    "mistral": RuntimeSpec("mistral/", key_env="MISTRAL_API_KEY"),
    "deepseek": RuntimeSpec("deepseek/", key_env="DEEPSEEK_API_KEY"),
    # Escape hatch: the model string is passed to LiteLLM untouched.
    "litellm": RuntimeSpec(""),
}

# Placeholder key for local OpenAI-compatible servers: it must never fall back to the real
# OPENAI_API_KEY, which LiteLLM would otherwise send to whatever api_base is configured.
LOCAL_API_KEY = "sk-local-no-key"
HEALTH_CACHE_SECONDS = 10.0


@dataclass
class ResolvedTier:
    name: str
    tier: str
    runtime: str
    model: str
    litellm_model: str
    api_base: str | None
    api_key_env: str | None
    local: bool
    timeout: float | None
    health_url: str | None
    _health_cache: tuple[float, bool, str] | None = field(default=None, repr=False)
    price_in: float | None = None
    price_out: float | None = None

    def call_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.litellm_model}
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key_env:
            kwargs["api_key"] = provider_secret(self.api_key_env)
        elif self.local:
            kwargs["api_key"] = LOCAL_API_KEY
        if self.timeout:
            kwargs["timeout"] = self.timeout
        return kwargs

    async def availability(self, probe_timeout: float = 1.5) -> tuple[bool, str]:
        if not self.local:
            if self.api_key_env and not provider_secret(self.api_key_env):
                return False, f"{self.api_key_env} is not set"
            return True, "ready"

        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < HEALTH_CACHE_SECONDS:
            return self._health_cache[1], self._health_cache[2]
        available, reason = await self._probe(probe_timeout)
        self._health_cache = (now, available, reason)
        return available, reason

    async def _probe(self, timeout: float) -> tuple[bool, str]:
        if not self.health_url:
            return True, "ready"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(self.health_url)
        except httpx.HTTPError as exc:
            return False, f"{self.runtime} unreachable at {self.api_base} ({type(exc).__name__})"
        if response.status_code >= 400:
            return False, f"{self.runtime} health check returned HTTP {response.status_code}"

        if self.runtime == "ollama":
            names = {item.get("name", "") for item in response.json().get("models", [])}
            wanted = {self.model, f"{self.model}:latest"}
            if not names & wanted:
                return False, f"model '{self.model}' is not pulled (run: magent llm pull {self.model})"
        return True, "ready"

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier,
            "runtime": self.runtime,
            "model": self.model,
            "local": self.local,
            "price_in": self.price_in,
            "price_out": self.price_out,
        }


def resolve_tier(config: TierConfig) -> ResolvedTier:
    spec = RUNTIMES.get(config.runtime)
    if spec is None:
        raise ValueError(f"tier '{config.name}': unknown runtime '{config.runtime}', choose one of {sorted(RUNTIMES)}")

    api_base = (config.api_base or spec.default_api_base or None)
    if api_base:
        api_base = api_base.rstrip("/")
    health_url = f"{api_base}{spec.health_path}" if (spec.local and api_base and spec.health_path) else None
    return ResolvedTier(
        name=config.name,
        tier=config.tier,
        runtime=config.runtime,
        model=config.model,
        litellm_model=f"{spec.prefix}{config.model}",
        api_base=api_base,
        api_key_env=config.api_key_env or spec.key_env,
        local=spec.local,
        timeout=config.timeout,
        health_url=health_url,
        price_in=config.price_in,
        price_out=config.price_out,
    )


def resolve_chain(configs: list[TierConfig]) -> list[ResolvedTier]:
    return [resolve_tier(config) for config in configs]
