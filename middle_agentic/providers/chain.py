"""Fallback chain: tries tiers in order until one answers.

Requested model selection (the OpenAI ``model`` field):
    "auto" / "middle-agent" / empty  -> whole chain in configured order
    a tier name   (e.g. "local")     -> only that tier
    a tier label  (free|local|paid)  -> every tier with that label, in order
Anything else is rejected, so clients cannot make the gateway call arbitrary paid models.
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import litellm

from middle_agentic.providers.registry import ResolvedTier

logger = logging.getLogger("middle_agentic.chain")

AUTO_MODELS = frozenset({"", "auto", "middle-agent"})

# Only the tier decides where a request goes and with which credentials.
ROUTING_KEYS = frozenset({
    "model", "messages", "stream", "api_base", "base_url", "api_key", "api_version",
    "custom_llm_provider", "timeout", "num_retries", "extra_headers", "headers",
})

# Drop provider-unsupported params instead of failing, and keep LiteLLM's own logs quiet.
litellm.drop_params = True
litellm.suppress_debug_info = True

# Errors that mean "this tier cannot serve the request right now, try the next one".
FALLBACK_ERRORS: tuple[type[BaseException], ...] = (
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.BadRequestError,
    litellm.InternalServerError,
    litellm.APIError,
)


class UnknownModelError(ValueError):
    pass


class AllTiersFailedError(Exception):
    def __init__(self, attempts: list[dict[str, Any]]) -> None:
        super().__init__("All model tiers failed or are unavailable")
        self.attempts = attempts


@dataclass
class StreamResult:
    tier: ResolvedTier
    attempts: list[dict[str, Any]]
    chunks: AsyncIterator[Any]


class ModelRouter:
    def __init__(self, tiers: list[ResolvedTier], request_timeout: float = 60.0, probe_timeout: float = 1.5) -> None:
        self.tiers = tiers
        self.request_timeout = request_timeout
        self.probe_timeout = probe_timeout

    def select(self, requested: str | None) -> list[ResolvedTier]:
        key = (requested or "").strip()
        if key in AUTO_MODELS:
            return list(self.tiers)
        by_name = [tier for tier in self.tiers if tier.name == key]
        if by_name:
            return by_name
        by_label = [tier for tier in self.tiers if tier.tier == key]
        if by_label:
            return by_label
        allowed = sorted({"auto", *(t.name for t in self.tiers), *(t.tier for t in self.tiers)})
        raise UnknownModelError(f"unknown model '{key}', allowed values: {allowed}")

    async def describe(self) -> list[dict[str, Any]]:
        described = []
        for tier in self.tiers:
            available, reason = await tier.availability(self.probe_timeout)
            described.append({**tier.describe(), "available": available, "reason": reason})
        return described

    def _call_kwargs(self, tier: ResolvedTier, messages: list[dict[str, Any]], params: dict[str, Any]) -> dict[str, Any]:
        kwargs = tier.call_kwargs()
        kwargs.setdefault("timeout", self.request_timeout)
        generation = {key: value for key, value in params.items() if key not in ROUTING_KEYS}
        # num_retries=0: the chain itself is the retry strategy; waiting on a rate-limited tier wastes time.
        return {**generation, **kwargs, "messages": messages, "num_retries": 0}

    async def complete(
        self, messages: list[dict[str, Any]], params: dict[str, Any], requested: str | None = None
    ) -> tuple[Any, ResolvedTier, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        for tier in self.select(requested):
            if not await self._is_available(tier, attempts):
                continue
            started = time.perf_counter()
            try:
                response = await litellm.acompletion(**self._call_kwargs(tier, messages, params))
            except Exception as exc:  # noqa: BLE001 - classified in _record_failure
                self._record_failure(tier, exc, started, attempts)
                continue
            self._record_success(tier, started, attempts)
            return response, tier, attempts
        raise AllTiersFailedError(attempts)

    async def stream(
        self, messages: list[dict[str, Any]], params: dict[str, Any], requested: str | None = None
    ) -> StreamResult:
        """Open a stream on the first tier that yields a chunk; fallback only happens before that."""
        attempts: list[dict[str, Any]] = []
        for tier in self.select(requested):
            if not await self._is_available(tier, attempts):
                continue
            started = time.perf_counter()
            try:
                response = await litellm.acompletion(**self._call_kwargs(tier, messages, params), stream=True)
                iterator = response.__aiter__()
                first_chunk = await iterator.__anext__()
            except StopAsyncIteration:
                self._record_failure(tier, RuntimeError("empty stream"), started, attempts)
                continue
            except Exception as exc:  # noqa: BLE001 - classified in _record_failure
                self._record_failure(tier, exc, started, attempts)
                continue
            self._record_success(tier, started, attempts)
            return StreamResult(tier=tier, attempts=attempts, chunks=_prepend(first_chunk, iterator))
        raise AllTiersFailedError(attempts)

    async def _is_available(self, tier: ResolvedTier, attempts: list[dict[str, Any]]) -> bool:
        available, reason = await tier.availability(self.probe_timeout)
        if not available:
            attempts.append({"name": tier.name, "model": tier.model, "tier": tier.tier, "status": "skipped", "reason": reason})
        return available

    @staticmethod
    def _record_failure(tier: ResolvedTier, exc: BaseException, started: float, attempts: list[dict[str, Any]]) -> None:
        if isinstance(exc, FALLBACK_ERRORS):
            logger.warning("tier %s (%s) failed: %s", tier.name, tier.litellm_model, type(exc).__name__)
        else:
            logger.exception("tier %s (%s) raised an unexpected error", tier.name, tier.litellm_model, exc_info=exc)
        attempts.append({
            "name": tier.name,
            "model": tier.model,
            "tier": tier.tier,
            "status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })

    @staticmethod
    def _record_success(tier: ResolvedTier, started: float, attempts: list[dict[str, Any]]) -> None:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        attempts.append({"name": tier.name, "model": tier.model, "tier": tier.tier, "status": "ok", "latency_ms": elapsed_ms})
        logger.info("served by tier %s (%s) in %d ms", tier.name, tier.litellm_model, elapsed_ms)


async def _prepend(first: Any, rest: AsyncIterator[Any]) -> AsyncIterator[Any]:
    yield first
    async for chunk in rest:
        yield chunk
