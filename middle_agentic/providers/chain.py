"""Fallback chain: tries tiers in order until one answers.

Requested model selection (the OpenAI ``model`` field):
    "auto" / "middle-agent" / empty  -> whole chain in configured order
    a tier name   (e.g. "local")     -> only that tier
    a tier label  (free|local|paid)  -> every tier with that label, in order
Anything else is rejected, so clients cannot make the gateway call arbitrary paid models.

Every attempt that reaches a provider is written to the usage ledger when one is attached
(see middle_agentic.server.usage). Callers pass ``ctx`` (key name, job label, stream flag) and may
pass ``tiers`` to run only a subset, which is how the daily budget downgrades a request.
"""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import litellm

from middle_agentic.providers.registry import ResolvedTier
from middle_agentic.server.usage import UsageLedger, price_call, tokens_from_usage

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
    messages: list[dict[str, Any]] = field(default_factory=list)
    ctx: dict[str, Any] | None = None
    started: float = 0.0


def _estimate_prompt_tokens(tier: ResolvedTier, messages: list[dict[str, Any]]) -> int:
    try:
        return int(litellm.token_counter(model=tier.litellm_model, messages=messages))
    except Exception:  # noqa: BLE001 - rough fallback when the tokenizer is unavailable
        return sum(len(str(message.get("content", ""))) for message in messages) // 4


class ModelRouter:
    def __init__(
        self,
        tiers: list[ResolvedTier],
        request_timeout: float = 60.0,
        probe_timeout: float = 1.5,
        ledger: UsageLedger | None = None,
    ) -> None:
        self.tiers = tiers
        self.request_timeout = request_timeout
        self.probe_timeout = probe_timeout
        self.ledger = ledger

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

    def _call_kwargs(
        self, tier: ResolvedTier, messages: list[dict[str, Any]], params: dict[str, Any], *, stream: bool = False
    ) -> dict[str, Any]:
        kwargs = tier.call_kwargs()
        kwargs.setdefault("timeout", self.request_timeout)
        generation = {key: value for key, value in params.items() if key not in ROUTING_KEYS}
        # num_retries=0: the chain itself is the retry strategy; waiting on a rate-limited tier wastes time.
        extra = {"stream_options": {"include_usage": True}} if stream else {}
        return {**generation, **extra, **kwargs, "messages": messages, "num_retries": 0}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        requested: str | None = None,
        *,
        tiers: list[ResolvedTier] | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> tuple[Any, ResolvedTier, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        candidates = tiers if tiers is not None else self.select(requested)
        for tier in candidates:
            if not await self._is_available(tier, attempts):
                continue
            started = time.perf_counter()
            try:
                response = await litellm.acompletion(**self._call_kwargs(tier, messages, params))
            except Exception as exc:  # noqa: BLE001 - classified in _record_failure
                self._record_failure(tier, exc, started, attempts)
                self._ledger_failure(tier, ctx, started, stream=False)
                continue
            self._record_success(tier, started, attempts)
            self._ledger_success(tier, ctx, response, started)
            return response, tier, attempts
        raise AllTiersFailedError(attempts)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        requested: str | None = None,
        *,
        tiers: list[ResolvedTier] | None = None,
        ctx: dict[str, Any] | None = None,
    ) -> StreamResult:
        """Open a stream on the first tier that yields a chunk; fallback only happens before that."""
        attempts: list[dict[str, Any]] = []
        candidates = tiers if tiers is not None else self.select(requested)
        for tier in candidates:
            if not await self._is_available(tier, attempts):
                continue
            started = time.perf_counter()
            try:
                response = await litellm.acompletion(**self._call_kwargs(tier, messages, params, stream=True), stream=True)
                iterator = response.__aiter__()
                first_chunk = await iterator.__anext__()
            except StopAsyncIteration:
                self._record_failure(tier, RuntimeError("empty stream"), started, attempts)
                self._ledger_failure(tier, ctx, started, stream=True)
                continue
            except Exception as exc:  # noqa: BLE001 - classified in _record_failure
                self._record_failure(tier, exc, started, attempts)
                self._ledger_failure(tier, ctx, started, stream=True)
                continue
            self._record_success(tier, started, attempts)
            return StreamResult(
                tier=tier,
                attempts=attempts,
                chunks=_prepend(first_chunk, iterator),
                messages=messages,
                ctx=ctx,
                started=started,
            )
        raise AllTiersFailedError(attempts)

    def record_stream(self, result: StreamResult, *, text: str, usage: Any, status: str) -> None:
        """Ledger row for a finished stream. Usage comes from the provider's final chunk when it sent one."""
        if self.ledger is None or result.ctx is None:
            return
        tier = result.tier
        prompt, completion = tokens_from_usage(usage)
        estimated = prompt is None or completion is None
        if estimated:
            prompt = _estimate_prompt_tokens(tier, result.messages)
            completion = len(text) // 4
        cost, source = price_call(
            tier_label=tier.tier, price_in=tier.price_in, price_out=tier.price_out,
            prompt_tokens=prompt, completion_tokens=completion, litellm_model=tier.litellm_model,
        )
        if estimated and source == "tier":
            source = "tier-estimated"
        self._write_ledger(
            result.ctx, tier, prompt=prompt, completion=completion, cost=cost, source=source,
            started=result.started, status=status, stream=True,
        )

    async def _is_available(self, tier: ResolvedTier, attempts: list[dict[str, Any]]) -> bool:
        available, reason = await tier.availability(self.probe_timeout)
        if not available:
            attempts.append({"name": tier.name, "model": tier.model, "tier": tier.tier, "status": "skipped", "reason": reason})
        return available

    # -- usage ledger --------------------------------------------------------------------
    def _ledger_success(self, tier: ResolvedTier, ctx: dict[str, Any] | None, response: Any, started: float) -> None:
        if self.ledger is None or ctx is None:
            return
        prompt, completion = tokens_from_usage(getattr(response, "usage", None))
        cost, source = price_call(
            tier_label=tier.tier, price_in=tier.price_in, price_out=tier.price_out,
            prompt_tokens=prompt, completion_tokens=completion, litellm_model=tier.litellm_model, response=response,
        )
        self._write_ledger(
            ctx, tier, prompt=prompt or 0, completion=completion or 0, cost=cost, source=source,
            started=started, status="ok", stream=False,
        )

    def _ledger_failure(self, tier: ResolvedTier, ctx: dict[str, Any] | None, started: float, *, stream: bool) -> None:
        if self.ledger is None or ctx is None:
            return
        self._write_ledger(ctx, tier, prompt=0, completion=0, cost=0.0, source="none", started=started, status="failed", stream=stream)

    def _write_ledger(
        self,
        ctx: dict[str, Any],
        tier: ResolvedTier,
        *,
        prompt: int,
        completion: int,
        cost: float,
        source: str,
        started: float,
        status: str,
        stream: bool,
    ) -> None:
        if self.ledger is None:
            return
        try:
            self.ledger.record(
                key=ctx.get("key", "-"),
                job=ctx.get("job", "-"),
                tier_name=tier.name,
                tier_label=tier.tier,
                model=tier.model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=cost,
                cost_src=source,
                latency_ms=round((time.perf_counter() - started) * 1000),
                status=status,
                stream=stream,
            )
        except Exception:  # noqa: BLE001 - accounting must never fail a request
            logger.exception("could not write usage row for tier %s", tier.name)

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
