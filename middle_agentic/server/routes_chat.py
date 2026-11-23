"""OpenAI-compatible chat endpoint wrapped with graph-memory pre/post hooks."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from middle_agentic.memory.graph import format_context, normalise_session_id
from middle_agentic.providers.chain import AllTiersFailedError, StreamResult, UnknownModelError
from middle_agentic.server.audit import current_key
from middle_agentic.server.auth import require_scopes
from middle_agentic.server.budget import decide, effective_budget
from middle_agentic.server.state import GatewayState, get_state
from middle_agentic.server.usage import UsageLedger

logger = logging.getLogger("middle_agentic.server.chat")

router = APIRouter(prefix="/v1", dependencies=[Depends(require_scopes("models"))])

# Client-supplied params forwarded to the provider. Anything else (api_key, api_base, ...) is
# dropped so a caller can never redirect the gateway's credentials to another endpoint.
FORWARDED_PARAMS = frozenset({
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "n",
    "presence_penalty", "frequency_penalty", "seed", "logit_bias", "logprobs", "top_logprobs",
    "tools", "tool_choice", "parallel_tool_calls", "response_format", "user", "reasoning_effort",
})


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = Field(default="auto", description="auto, a chain tier name or a tier label (free|local|paid)")
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False


def message_text(content: Any) -> str:
    """Flatten OpenAI message content (string or multimodal parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return str(content)


def last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return message_text(message.get("content"))
    return ""


async def pre_hook(state: GatewayState, session_id: str, messages: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Inject remembered context as a system message placed after the client's own system prompts."""
    try:
        rows = await state.memory.fetch_context(session_id, query)
    except sqlite3.Error:
        logger.exception("pre-hook: memory lookup failed, continuing without context")
        return messages
    context = format_context(rows, state.settings.memory.snippet_chars)
    if not context:
        return messages

    insert_at = 0
    while insert_at < len(messages) and messages[insert_at].get("role") == "system":
        insert_at += 1
    return [*messages[:insert_at], {"role": "system", "content": context}, *messages[insert_at:]]


async def post_hook(state: GatewayState, session_id: str, user_text: str, assistant_text: str, metadata: dict[str, Any]) -> None:
    """Runs after the response has been sent; must never raise."""
    try:
        await state.memory.record_exchange(session_id, user_text, assistant_text, metadata)
    except Exception:  # noqa: BLE001 - background work, log and swallow
        logger.exception("post-hook: failed to record exchange for session %s", session_id)


def _all_failed_response(exc: AllTiersFailedError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": {"message": str(exc), "type": "all_tiers_failed", "attempts": exc.attempts}},
    )


def _gateway_headers(
    state: GatewayState, tier_name: str, tier_label: str, model: str, session_id: str, attempts: int, budget: str = "ok"
) -> dict[str, str]:
    return {
        "X-Gateway-Node": state.settings.node.name,
        "X-Gateway-Tier-Name": tier_name,
        "X-Gateway-Tier": tier_label,
        "X-Gateway-Model": model,
        "X-Gateway-Session": session_id,
        "X-Gateway-Attempts": str(attempts),
        "X-Gateway-Budget": budget,
    }


def _parse_budget_header(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="X-Budget-USD must be a number of US dollars") from exc
    if not math.isfinite(number) or number < 0:
        raise HTTPException(status_code=400, detail="X-Budget-USD must be zero or positive")
    return number


def _job_label(value: str | None) -> str:
    label = (value or "").strip()
    return label[:64] if label else "-"


def _budget_exceeded_response(state: GatewayState, spent: float, budget: float | None) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"X-Gateway-Budget": "blocked", "Retry-After": "3600"},
        content={
            "error": {
                "message": "daily budget reached; requests resume after local midnight",
                "type": "budget_exceeded",
                "spent_usd": round(spent, 6),
                "budget_usd": budget,
                "on_exceed": state.settings.budget.on_exceed,
            }
        },
    )


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    state = get_state(request)
    created = int(time.time())
    data = [{"id": "auto", "object": "model", "created": created, "owned_by": state.settings.node.name}]
    for tier in await state.router.describe():
        data.append({
            "id": tier["name"],
            "object": "model",
            "created": created,
            "owned_by": tier["runtime"],
            "tier": tier["tier"],
            "model": tier["model"],
            "available": tier["available"],
        })
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    x_session_id: str | None = Header(default=None),
    x_memory: str | None = Header(default=None),
    x_job: str | None = Header(default=None),
    x_budget_usd: str | None = Header(default=None),
) -> Any:
    state = get_state(request)
    session_id = normalise_session_id(x_session_id)
    use_memory = state.memory_enabled and (x_memory or "").strip().lower() != "off"
    messages = [message.model_dump(exclude_none=True) for message in body.messages]
    user_text = last_user_text(messages)
    request_budget = _parse_budget_header(x_budget_usd)

    try:
        candidates = state.router.select(body.model)
    except UnknownModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    key_name = current_key() or "-"
    budget = effective_budget(state.settings.budget.daily_usd, request_budget)
    spent = state.usage.spent_on(UsageLedger.today(), key_name)
    decision = decide(candidates, spent_usd=spent, budget_usd=budget, on_exceed=state.settings.budget.on_exceed)
    if decision.status == "blocked":
        return _budget_exceeded_response(state, spent, budget)
    ctx = {"key": key_name, "job": _job_label(x_job), "stream": body.stream}

    if use_memory and user_text:
        messages = await pre_hook(state, session_id, messages, user_text)

    params = {key: value for key, value in (body.model_extra or {}).items() if key in FORWARDED_PARAMS}

    if body.stream:
        try:
            result = await state.router.stream(messages, params, tiers=decision.tiers, ctx=ctx)
        except AllTiersFailedError as exc:
            return _all_failed_response(exc)
        headers = _gateway_headers(
            state, result.tier.name, result.tier.tier, result.tier.model, session_id, len(result.attempts), decision.status
        )
        return StreamingResponse(
            _sse_events(state, result, session_id, user_text if use_memory else ""),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache"},
        )

    try:
        response, tier, attempts = await state.router.complete(messages, params, tiers=decision.tiers, ctx=ctx)
    except AllTiersFailedError as exc:
        return _all_failed_response(exc)

    payload = jsonable_encoder(response.model_dump())
    choices = payload.get("choices") or [{}]
    assistant_text = message_text((choices[0].get("message") or {}).get("content"))

    if use_memory and user_text and assistant_text:
        background_tasks.add_task(
            post_hook, state, session_id, user_text, assistant_text,
            {"tier": tier.name, "model": tier.model, "usage": payload.get("usage")},
        )
    headers = _gateway_headers(state, tier.name, tier.tier, tier.model, session_id, len(attempts), decision.status)
    return JSONResponse(content=payload, headers=headers)


async def _sse_events(state: GatewayState, result: StreamResult, session_id: str, user_text: str) -> AsyncIterator[str]:
    parts: list[str] = []
    usage: Any = None
    outcome = "aborted"  # stays this way if the client disconnects mid-stream
    try:
        try:
            async for chunk in result.chunks:
                usage = getattr(chunk, "usage", None) or usage
                data = jsonable_encoder(chunk.model_dump() if hasattr(chunk, "model_dump") else chunk)
                for choice in data.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        parts.append(delta)
                yield f"data: {json.dumps(data)}\n\n"
            outcome = "ok"
        except Exception as exc:  # noqa: BLE001 - headers are already sent, report in-band
            outcome = "failed"
            logger.exception("stream from tier %s broke mid-response", result.tier.name)
            yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'stream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        state.router.record_stream(result, text="".join(parts), usage=usage, status=outcome)

    if user_text and parts:
        state.spawn(post_hook(
            state, session_id, user_text, "".join(parts),
            {"tier": result.tier.name, "model": result.tier.model, "stream": True},
        ))
