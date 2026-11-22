from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Header, HTTPException, Request

from middle_agentic.server.api_keys import Denied
from middle_agentic.server.audit import note
from middle_agentic.server.state import get_state


def bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def require_scopes(*needed: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency: a known, unexpired key within its quota that holds every scope in `needed`.

    Unknown, revoked or expired key: 401. Over quota: 429. Known key without the scope: 403.
    """

    async def check(request: Request, authorization: str | None = Header(default=None)) -> None:
        try:
            record = get_state(request).keys.authorize(bearer_token(authorization))
        except Denied as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
            raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=headers) from exc
        note(key=record.name)
        missing = [scope for scope in needed if scope not in record.scopes]
        if missing:
            raise HTTPException(status_code=403, detail=f"this key lacks scope: {', '.join(missing)}")

    return check


require_api_key = require_scopes()
