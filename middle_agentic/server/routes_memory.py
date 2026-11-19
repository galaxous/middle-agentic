"""Direct memory API: pre/post hook primitives usable without calling an LLM."""
from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from middle_agentic.memory.graph import format_context, normalise_session_id
from middle_agentic.server.auth import require_api_key
from middle_agentic.server.state import GatewayState, require_memory

router = APIRouter(prefix="/v1/memory", dependencies=[Depends(require_api_key)])


class ContextRequest(BaseModel):
    session_id: str = "default"
    query: str = ""
    recent_limit: int | None = Field(default=None, ge=0, le=100)
    related_limit: int | None = Field(default=None, ge=0, le=100)
    important_limit: int | None = Field(default=None, ge=0, le=100)


class EventRequest(BaseModel):
    session_id: str = "default"
    role: str = Field(default="note", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    content: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    link_to: str | None = Field(default=None, description="Existing node id to link the new node from")
    relation: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")


@router.post("/context")
async def memory_context(body: ContextRequest, state: GatewayState = Depends(require_memory)) -> dict[str, Any]:
    session_id = normalise_session_id(body.session_id)
    rows = await state.memory.fetch_context(
        session_id, body.query, body.recent_limit, body.related_limit, body.important_limit
    )
    return {
        "session_id": session_id,
        "nodes": rows,
        "context": format_context(rows, state.settings.memory.snippet_chars),
    }


@router.post("/events", status_code=201)
async def memory_event(body: EventRequest, state: GatewayState = Depends(require_memory)) -> dict[str, Any]:
    session_id = normalise_session_id(body.session_id)
    try:
        node_id = await state.memory.add_node(
            session_id, body.role, body.content, body.metadata, link_to=body.link_to, relation=body.relation
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=400, detail=f"link_to node does not exist: {body.link_to}") from exc
    return {"id": node_id, "session_id": session_id}


class ForgetRequest(BaseModel):
    session_id: str = "default"
    ids: list[str] = Field(default_factory=list, max_length=1000)
    match: str | None = Field(default=None, min_length=3, max_length=1000)
    role: str | None = Field(default=None, pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    with_answers: bool = True
    dry_run: bool = True


@router.post("/forget")
async def memory_forget(body: ForgetRequest, state: GatewayState = Depends(require_memory)) -> dict[str, Any]:
    session_id = normalise_session_id(body.session_id)
    try:
        nodes = await state.memory.forget(
            session_id, body.ids, body.match, body.role, body.with_answers, body.dry_run
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": session_id, "deleted": not body.dry_run, "nodes": nodes}


@router.get("/sessions")
async def memory_sessions(
    limit: int = Query(default=50, ge=1, le=500), state: GatewayState = Depends(require_memory)
) -> dict[str, Any]:
    return {"sessions": await state.memory.list_sessions(limit)}


@router.get("/sessions/{session_id}")
async def memory_session_graph(
    session_id: str, limit: int = Query(default=200, ge=1, le=2000), state: GatewayState = Depends(require_memory)
) -> dict[str, Any]:
    return await state.memory.session_graph(normalise_session_id(session_id), limit)
