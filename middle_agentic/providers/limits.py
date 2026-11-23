"""Per-tier concurrency limits for the gateway process.

A tier with a limit allows that many requests in flight at once. A request that cannot get a slot
within the tier's queue_wait is skipped and the chain moves on, so a busy local model never holds
up a request that a cloud tier could serve. release() is synchronous so it can be called from
cleanup code (for example when a stream finishes).
"""
from __future__ import annotations

import asyncio
import time


class ConcurrencyLimiter:
    POLL_SECONDS = 0.02

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.in_use = 0

    def resize(self, limit: int | None) -> None:
        """Takes effect for the next acquire; requests already running keep their slots."""
        self.limit = limit

    def _try_acquire(self) -> bool:
        if self.limit is None or self.in_use < self.limit:
            self.in_use += 1
            return True
        return False

    async def acquire(self, wait: float) -> bool:
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            if self._try_acquire():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.POLL_SECONDS)

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)
