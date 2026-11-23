"""Fallback chain: tries tiers in order until one answers.

Requested model selection (the OpenAI ``model`` field):
    "auto" / "middle-agent" / empty  -> whole chain in configured order
    a tier name   (e.g. "local")     -> only that tier
    a tier label  (free|local|paid)  -> every tier with that label, in order
Anything else is rejected, so clients cannot make the gateway call arbitrary paid models.

Live changes: when the router is given the base config and an override store, the effective chain
(enabled tiers, order, model, timeout, prices, limits) is rebuilt whenever the store changes. Nothing
needs a restart.

Concurrency: each tier has a limiter (see providers/limits.py). A request waits up to the tier's
queue_wait for a slot, then the chain moves on. Streams keep their slot until the stream finishes.

Every attempt that reaches a provider is written to the usage ledger when one is attached
(see middle_agentic.server.usage). Callers pass ``ctx`` (key name, job label, stream flag) and may
pass ``tiers`` to run only a subset, which is how the daily budget downgrades a request.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import litellm

from middle_agentic.config import TierConfig
from middle_agentic.providers.limits import ConcurrencyLimiter
from middle_agentic.providers.registry import ResolvedTier, resolve_chain
from middle_agentic.server.usage import UsageLedger, price_call, tokens_from_usage

if TYPE_CHECKING:  # pragma: no cover
    from middle_agentic.server.overrides import OverrideStore

logger = logging.getLogger("middle_agentic.chain")

AUTO_MODELS = frozenset({"", "auto", "middle-agent"})
PROFILE_PREFIX = "profile:"
PROFILES = ("cheap-first", "quality-first")

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
    release: Callable[[], None] | None = None


def _estimate_prompt_tokens(tier: ResolvedTier, messages: list[dict[str, Any]]) -> int:
    try:
        return int(litellm.token_counter(model=tier.litellm_model, messages=messages))
    except Exception:  # noqa: BLE001 - rough fallback when the tokenizer is unavailable
        return sum(len(str(message.get("content", ""))) for message in messages) // 4


class ModelRouter:
    def __init__(
        self,
        tiers: list[ResolvedTier] | None = None,
        request_timeout: float = 60.0,
        probe_timeout: float = 1.5,
        ledger: UsageLedger | None = None,
        *,
        configs: list[TierConfig] | None = None,
        overrides: "OverrideStore | None" = None,
    ) -> None:
        self.request_timeout = request_timeout
        self.probe_timeout = probe_timeout
        self.ledger = ledger
        self._configs = configs
        self._overrides = overrides
        self._static_tiers = list(tiers or [])
        self._effective: list[ResolvedTier] | None = None
        self._effective_version: Any = None
        self._limiters: dict[str, ConcurrencyLimiter] = {}
        self._cooldown: dict[str, float] = {}  # tier name -> monotonic time when it may be tried again

    # -- chain ---------------------------------------------------------------------------
    @property
    def base_configs(self) -> list[TierConfig]:
        return list(self._configs or [])

    def current_tiers(self) -> list[ResolvedTier]:
        """The chain as it stands now: base config plus runtime overrides, rebuilt only when they change."""
        if self._configs is None:
            return self._static_tiers
        version = self._overrides.version() if self._overrides is not None else None
        if self._effective is None or version != self._effective_version:
            from middle_agentic.server.overrides import effective_configs

            overrides = self._overrides.tier_overrides() if self._overrides is not None else {}
            order = self._overrides.order() if self._overrides is not None else []
            self._effective = resolve_chain(effective_configs(self._configs, overrides, order))
            self._effective_version = version
        return self._effective

    @property
    def tiers(self) -> list[ResolvedTier]:
        return self.current_tiers()

    def select(self, requested: str | None) -> list[ResolvedTier]:
        tiers = self.current_tiers()
        key = (requested or "").strip()
        if key.startswith(PROFILE_PREFIX):
            profile = key[len(PROFILE_PREFIX):]
            if profile not in PROFILES:
                raise UnknownModelError(f"unknown profile '{profile}', choose one of {list(PROFILES)}")
            return _order_for_profile(tiers, profile)
        if key in AUTO_MODELS:
            return list(tiers)
        by_name = [tier for tier in tiers if tier.name == key]
        if by_name:
            return by_name
        by_label = [tier for tier in tiers if tier.tier == key]
        if by_label:
            return by_label
        allowed = sorted({"auto", *(f"{PROFILE_PREFIX}{p}" for p in PROFILES), *(t.name for t in tiers), *(t.tier for t in tiers)})
        raise UnknownModelError(f"unknown model '{key}', allowed values: {allowed}")

    async def describe(self) -> list[dict[str, Any]]:
        described = []
        for tier in self.current_tiers():
            available, reason = await tier.availability(self.probe_timeout)
            described.append({**tier.describe(), "available": available, "reason": reason})
        return described

    def in_flight(self, name: str) -> int:
        limiter = self._limiters.get(name)
        return limiter.in_use if limiter is not None else 0

    def _limiter(self, tier: ResolvedTier) -> ConcurrencyLimiter:
        limiter = self._limiters.get(tier.name)
        if limiter is None:
            limiter = self._limiters[tier.name] = ConcurrencyLimiter(tier.max_concurrency)
        elif limiter.limit != tier.max_concurrency:
            limiter.resize(tier.max_concurrency)
        return limiter

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
            if not await self._admit(tier, attempts, messages, params):
                continue
            limiter = self._limiter(tier)
            if not await limiter.acquire(tier.queue_wait):
                attempts.append(_saturated(tier))
                continue
            try:
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
            finally:
                limiter.release()
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
        """Open a stream on the first tier that yields a chunk; fallback only happens before that.

        The tier's slot is held until record_stream() runs, i.e. until the stream has ended.
        """
        attempts: list[dict[str, Any]] = []
        candidates = tiers if tiers is not None else self.select(requested)
        for tier in candidates:
            if not await self._admit(tier, attempts, messages, params):
                continue
            limiter = self._limiter(tier)
            if not await limiter.acquire(tier.queue_wait):
                attempts.append(_saturated(tier))
                continue
            started = time.perf_counter()
            try:
                response = await litellm.acompletion(**self._call_kwargs(tier, messages, params, stream=True), stream=True)
                iterator = response.__aiter__()
                first_chunk = await iterator.__anext__()
            except StopAsyncIteration:
                limiter.release()
                self._record_failure(tier, RuntimeError("empty stream"), started, attempts)
                self._ledger_failure(tier, ctx, started, stream=True)
                continue
            except Exception as exc:  # noqa: BLE001 - classified in _record_failure
                limiter.release()
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
                release=limiter.release,
            )
        raise AllTiersFailedError(attempts)

    def record_stream(self, result: StreamResult, *, text: str, usage: Any, status: str) -> None:
        """Ledger row for a finished stream, and release of its slot. Usage comes from the provider's final chunk when sent."""
        try:
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
        finally:
            if result.release is not None:
                result.release()
                result.release = None

    async def _admit(
        self, tier: ResolvedTier, attempts: list[dict[str, Any]], messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> bool:
        """Whether this tier may be tried for this request. A refusal is recorded as a skipped attempt with its reason."""
        reason = self._skip_reason(tier, messages, params)
        if reason is None:
            available, detail = await tier.availability(self.probe_timeout)
            if available:
                return True
            reason = detail
        attempts.append({"name": tier.name, "model": tier.model, "tier": tier.tier, "status": "skipped", "reason": reason})
        return False

    def _skip_reason(self, tier: ResolvedTier, messages: list[dict[str, Any]], params: dict[str, Any]) -> str | None:
        until = self._cooldown.get(tier.name)
        if until is not None:
            remaining = until - time.monotonic()
            if remaining > 0:
                return f"cooling down after a rate limit ({math.ceil(remaining)}s left)"
            self._cooldown.pop(tier.name, None)
        if tier.daily_request_cap is not None and self.ledger is not None:
            used = self.ledger.requests_since(tier.name, _day_start(tier.reset_tz))
            if used >= tier.daily_request_cap:
                return f"daily request cap reached ({used}/{tier.daily_request_cap})"
        if params.get("tools") and tier.supports_tools is False:
            return "capability: this tier does not support tool calls"
        if tier.context_window is not None:
            estimated = _rough_tokens(messages)
            if estimated > tier.context_window:
                return f"capability: prompt (~{estimated} tokens) exceeds the context window ({tier.context_window})"
        return None

    async def probe(self, name: str) -> dict[str, Any]:
        """One short request straight to a tier: skips the chain, the budget and the queue. For `magent models test`."""
        tier = next((t for t in self.current_tiers() if t.name == name), None)
        if tier is None:
            raise UnknownModelError(f"tier '{name}' is not in the active chain (disabled tiers must be enabled first)")
        messages = [{"role": "user", "content": "Sadece 'tamam' yaz"}]
        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**self._call_kwargs(tier, messages, {"max_tokens": 5}))
        except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
            self._ledger_failure(tier, {"key": "admin", "job": "models-test"}, started, stream=False)
            return {"ok": False, "tier": name, "model": tier.model, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "latency_ms": round((time.perf_counter() - started) * 1000)}
        prompt, completion = tokens_from_usage(getattr(response, "usage", None))
        cost, source = price_call(
            tier_label=tier.tier, price_in=tier.price_in, price_out=tier.price_out,
            prompt_tokens=prompt, completion_tokens=completion, litellm_model=tier.litellm_model, response=response,
        )
        self._write_ledger({"key": "admin", "job": "models-test"}, tier, prompt=prompt or 0, completion=completion or 0,
                           cost=cost, source=source, started=started, status="ok", stream=False)
        try:
            reply = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            reply = ""
        return {"ok": True, "tier": name, "model": tier.model, "reply": reply[:200],
                "latency_ms": round((time.perf_counter() - started) * 1000), "prompt_tokens": prompt,
                "completion_tokens": completion, "cost_usd": round(cost, 6), "cost_src": source}

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

    def _record_failure(self, tier: ResolvedTier, exc: BaseException, started: float, attempts: list[dict[str, Any]]) -> None:
        if isinstance(exc, litellm.RateLimitError):
            self._cooldown[tier.name] = time.monotonic() + tier.cooldown_seconds
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


def _saturated(tier: ResolvedTier) -> dict[str, Any]:
    return {
        "name": tier.name,
        "model": tier.model,
        "tier": tier.tier,
        "status": "skipped",
        "reason": f"saturated: {tier.max_concurrency} request(s) already running, waited {tier.queue_wait:g}s",
    }


async def _prepend(first: Any, rest: AsyncIterator[Any]) -> AsyncIterator[Any]:
    yield first
    async for chunk in rest:
        yield chunk


def _day_start(reset_tz: str | None) -> float:
    """Timestamp of midnight today in the tier's reset timezone (local time when none is given)."""
    now = datetime.now(ZoneInfo(reset_tz)) if reset_tz else datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _rough_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap estimate (about 4 characters per token); good enough to refuse a clearly oversized prompt."""
    return sum(len(str(message.get("content") or "")) for message in messages) // 4


def _cost_value(tier: ResolvedTier) -> float:
    """Price used for ordering: output price per 1M tokens. Unpriced free/local tiers cost 0; unpriced paid tiers sort last."""
    if tier.price_out is not None:
        return tier.price_out
    return 0.0 if tier.tier in ("free", "local") else math.inf


def _order_for_profile(tiers: list[ResolvedTier], profile: str) -> list[ResolvedTier]:
    if profile == "cheap-first":
        return sorted(tiers, key=_cost_value)
    return sorted(tiers, key=lambda tier: -_cost_value(tier))  # quality-first: most expensive first
