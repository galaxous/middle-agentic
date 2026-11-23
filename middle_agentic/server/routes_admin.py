"""Admin endpoints: usage and budget reports, the model chain, and live changes to both.

Every route needs the `admin` scope. Keys for clients or workers normally hold only `models` and
`memory:*`; admin is granted explicitly (`magent key create <name> --scopes ...,admin`).

Changes are stored as runtime overrides (see server/overrides.py) and take effect on the next request.
config.yaml is never rewritten. Every call is written to the audit log with the key name and path.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from middle_agentic.providers.chain import UnknownModelError
from middle_agentic.server.api_keys import ApiKeyError
from middle_agentic.server.auth import require_scopes
from middle_agentic.server.overrides import OverrideError
from middle_agentic.server.state import GatewayState, get_state
from middle_agentic.server.usage import UsageLedger

router = APIRouter(prefix="/admin", dependencies=[Depends(require_scopes("admin"))])


class ModelPatch(BaseModel):
    """Only the fields present in the request body are changed."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    model: str | None = None
    timeout: float | None = Field(default=None, gt=0)
    price_in: float | None = Field(default=None, ge=0)
    price_out: float | None = Field(default=None, ge=0)
    max_concurrency: int | None = Field(default=None, ge=0)
    queue_wait: float | None = Field(default=None, ge=0)


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(min_length=1)


class BudgetPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = Field(default=None, ge=0)
    on_exceed: str | None = Field(default=None, pattern="^(downgrade|block)$")


class KeyBudgetPatch(BaseModel):
    """Per-key cap. Give daily_usd and/or on_exceed, or clear=true to fall back to the gateway-wide budget."""

    model_config = ConfigDict(extra="forbid")

    daily_usd: float | None = Field(default=None, gt=0)
    on_exceed: str | None = Field(default=None, pattern="^(downgrade|block)$")
    clear: bool = False


def _known_names(state: GatewayState) -> set[str]:
    return {config.name for config in state.router.base_configs}


def _require_known(state: GatewayState, name: str) -> None:
    if name not in _known_names(state):
        raise HTTPException(status_code=404, detail=f"no tier named '{name}' in config.yaml")


def _bad_request(exc: OverrideError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# -- reports -----------------------------------------------------------------------------
@router.get("/usage")
async def usage(
    request: Request,
    by: str = Query(default="tier", pattern="^(tier|key|job|day)$"),
    days: int = Query(default=1, ge=1, le=366),
) -> dict[str, Any]:
    state = get_state(request)
    return {"by": by, "days": days, "data": state.usage.summary(by, days)}


@router.get("/budget")
async def budget(request: Request, key: str | None = Query(default=None)) -> dict[str, Any]:
    """Gateway-wide budget, or one key's budget and spend when ?key=NAME is given."""
    return _budget_report(get_state(request), key)


def _budget_report(state: GatewayState, key: str | None = None) -> dict[str, Any]:
    today = UsageLedger.today()
    daily, on_exceed = state.budget_policy()
    report: dict[str, Any] = {
        "day": today,
        "daily_usd": daily,  # 0 means no cap
        "spent_usd": round(state.usage.spent_on(today), 6),
        "on_exceed": on_exceed,
        "source": "runtime" if state.overrides.budget() else "config",
    }
    if key is not None:
        key_daily, key_on_exceed = state.keys.budget_for(key)
        report = {
            "day": today,
            "key": key,
            "key_budget_usd": key_daily,  # None = the gateway-wide budget applies to this key
            "key_on_exceed": key_on_exceed,
            "effective_daily_usd": key_daily if key_daily is not None else daily,
            "effective_on_exceed": key_on_exceed or on_exceed,
            "spent_usd": round(state.usage.spent_on(today, key), 6),
        }
    return report


@router.put("/keys/{name}/budget")
async def set_key_budget(name: str, body: KeyBudgetPatch, request: Request) -> dict[str, Any]:
    state = get_state(request)
    try:
        changed = state.keys.set_budget(name, body.daily_usd, body.on_exceed, clear=body.clear)
    except ApiKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail=f"no active key named '{name}'")
    return await set_key_budget_report(state, name)


async def set_key_budget_report(state: GatewayState, name: str) -> dict[str, Any]:
    daily, on_exceed = state.keys.budget_for(name)
    return {"key": name, "key_budget_usd": daily, "key_on_exceed": on_exceed}


@router.get("/models")
async def models(request: Request) -> dict[str, Any]:
    state = get_state(request)
    overrides = state.overrides.tier_overrides()
    order = state.overrides.order()
    live = {tier["name"]: tier for tier in await state.router.describe()}
    position = {name: index for index, name in enumerate(order)}

    rows: list[dict[str, Any]] = []
    for config in state.router.base_configs:
        name = config.name
        change = overrides.get(name, {})
        enabled = change.get("enabled", True)
        entry: dict[str, Any] = {
            "name": name,
            "tier": config.tier,
            "runtime": config.runtime,
            "model": change.get("model", config.model),
            "enabled": enabled,
            "position": position.get(name),
            "timeout": change.get("timeout", config.timeout),
            "price_in": change.get("price_in", config.price_in),
            "price_out": change.get("price_out", config.price_out),
            "max_concurrency": change.get("max_concurrency", config.max_concurrency),
            "queue_wait": change.get("queue_wait", config.queue_wait),
            "overridden": name in overrides or name in position,
            "in_flight": state.router.in_flight(name),
        }
        if name in live and enabled:
            entry["available"] = live[name]["available"]
            entry["reason"] = live[name]["reason"]
        else:
            entry["available"] = None
            entry["reason"] = "disabled" if not enabled else "not in chain"
        rows.append(entry)
    rows.sort(key=lambda row: (row["position"] is None, row["position"] or 0))
    return {"data": rows, "order": order}


# -- live changes ------------------------------------------------------------------------
@router.delete("/models/order")
async def clear_order(request: Request) -> dict[str, Any]:
    state = get_state(request)
    return {"reset": state.overrides.clear_order()}


@router.post("/models/{name}/test")
async def test_model(name: str, request: Request) -> dict[str, Any]:
    """One short request straight to the tier (no chain, budget or queue). Costs a little and is recorded."""
    state = get_state(request)
    _require_known(state, name)
    try:
        return await state.router.probe(name)
    except UnknownModelError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/models/{name}")
async def reset_model(name: str, request: Request) -> dict[str, Any]:
    state = get_state(request)
    _require_known(state, name)
    return {"name": name, "reset": state.overrides.clear_tier(name)}


@router.patch("/models/{name}")
async def patch_model(name: str, body: ModelPatch, request: Request) -> dict[str, Any]:
    state = get_state(request)
    _require_known(state, name)
    changes = body.model_dump(exclude_unset=True)
    try:
        state.overrides.set_tier(name, changes)
    except OverrideError as exc:
        raise _bad_request(exc) from exc
    return {"name": name, "override": state.overrides.tier_overrides().get(name, {})}


@router.put("/models/order")
async def set_order(body: OrderBody, request: Request) -> dict[str, Any]:
    state = get_state(request)
    for name in body.names:
        _require_known(state, name)
    try:
        state.overrides.set_order(body.names)  # rejects duplicates before anything is saved
    except OverrideError as exc:
        raise _bad_request(exc) from exc
    return {"order": body.names}


@router.delete("/overrides")
async def clear_all(request: Request) -> dict[str, Any]:
    """Back to config.yaml for every tier and the order. The budget override is kept; see DELETE /admin/budget."""
    state = get_state(request)
    return {"tiers_reset": state.overrides.clear_all_tiers(), "order_reset": state.overrides.clear_order()}


@router.put("/budget")
async def set_budget(body: BudgetPatch, request: Request) -> dict[str, Any]:
    state = get_state(request)
    try:
        state.overrides.set_budget(daily_usd=body.daily_usd, on_exceed=body.on_exceed)
    except OverrideError as exc:
        raise _bad_request(exc) from exc
    return _budget_report(state)


@router.delete("/budget")
async def clear_budget(request: Request) -> dict[str, Any]:
    state = get_state(request)
    state.overrides.clear_budget()
    return _budget_report(state)
