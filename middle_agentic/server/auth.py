from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException, Request

from middle_agentic.server.state import get_state


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_scopes(*needed: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency: the request needs a known key that holds every scope in `needed`.

    No key or an unknown/revoked key gives 401. A known key without the scope gives 403.
    """

    async def check(request: Request, authorization: str | None = Header(default=None)) -> None:
        scopes = get_state(request).keys.scopes_for(bearer_token(authorization))
        if scopes is None:
            raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})
        missing = [scope for scope in needed if scope not in scopes]
        if missing:
            raise HTTPException(status_code=403, detail=f"this key lacks scope: {', '.join(missing)}")

    return check


require_api_key = require_scopes()
