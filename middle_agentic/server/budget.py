"""Daily budget decisions, made before any model is called.

The effective limit for a request is the configured daily cap, lowered (never raised) by the client's
X-Budget-USD header. A header of 0 means no paid spending for that request.

Only free and local tiers are considered zero-cost. A tier with an unknown label ("custom") is treated
as paid when the budget is exhausted, so an unpriced model cannot keep spending past the cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from middle_agentic.providers.registry import ResolvedTier

Status = Literal["ok", "downgraded", "blocked"]
ZERO_COST_LABELS = frozenset({"free", "local"})


@dataclass(frozen=True)
class BudgetDecision:
    tiers: list[ResolvedTier]
    status: Status
    budget_usd: float | None
    spent_usd: float


def effective_budget(daily_usd: float, header_usd: float | None) -> float | None:
    """None means no limit. daily_usd == 0 disables the configured cap."""
    limit = daily_usd if daily_usd > 0 else None
    if header_usd is not None:
        limit = header_usd if limit is None else min(limit, header_usd)
    return limit


def _is_zero_cost(tier: ResolvedTier) -> bool:
    if tier.price_in or tier.price_out:
        return False
    return tier.tier in ZERO_COST_LABELS


def decide(
    candidates: list[ResolvedTier],
    *,
    spent_usd: float,
    budget_usd: float | None,
    on_exceed: str,
) -> BudgetDecision:
    if budget_usd is None or spent_usd < budget_usd:
        return BudgetDecision(candidates, "ok", budget_usd, spent_usd)
    if on_exceed == "block":
        return BudgetDecision([], "blocked", budget_usd, spent_usd)
    free = [tier for tier in candidates if _is_zero_cost(tier)]
    if free:
        return BudgetDecision(free, "downgraded", budget_usd, spent_usd)
    return BudgetDecision([], "blocked", budget_usd, spent_usd)
