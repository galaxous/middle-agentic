from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request

from middle_agentic.server.state import get_state


async def require_api_key(request: Request, authorization: str | None = Header(default=None)) -> None:
    expected = get_state(request).settings.gateway.api_key
    if not expected:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip().encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})
