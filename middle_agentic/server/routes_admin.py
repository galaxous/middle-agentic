"""Read-only admin endpoints: usage, budget and the model chain. Every route needs the `admin` scope.

Keys created for clients or workers normally hold only `models` and `memory:*`; admin is granted
explicitly (`magent key create <name> --scopes ...,admin`). Changing tiers or budgets at runtime is
not implemented yet, so these endpoints only report.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from middle_agentic.server.auth import require_scopes
from middle_agentic.server.state import get_state
from middle_agentic.server.usage import UsageLedger

router = APIRouter(prefix="/admin", dependencies=[Depends(require_scopes("admin"))])


@router.get("/usage")
async def usage(
    request: Request,
    by: str = Query(default="tier", pattern="^(tier|key|job|day)$"),
    days: int = Query(default=1, ge=1, le=366),
) -> dict[str, Any]:
    state = get_state(request)
    return {"by": by, "days": days, "data": state.usage.summary(by, days)}


@router.get("/budget")
async def budget(request: Request) -> dict[str, Any]:
    state = get_state(request)
    today = UsageLedger.today()
    config = state.settings.budget
    return {
        "day": today,
        "daily_usd": config.daily_usd,  # 0 means no cap
        "spent_usd": round(state.usage.spent_on(today), 6),
        "on_exceed": config.on_exceed,
    }


@router.get("/models")
async def models(request: Request) -> dict[str, Any]:
    state = get_state(request)
    described = await state.router.describe()
    # "enabled" is true for every tier in the chain; runtime overrides do not exist yet, so "overridden" is false.
    return {"data": [{**tier, "enabled": True, "overridden": False} for tier in described]}
