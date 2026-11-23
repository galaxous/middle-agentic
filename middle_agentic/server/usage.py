"""Usage ledger: one row per model attempt (tokens, cost, latency, outcome), plus daily spend for budgets.

Rows live in the same SQLite file as the audit log and the API keys. A failed attempt is recorded with
cost 0 so that rate limits and outages show up in reports without counting as spend.

Cost precedence for a successful call:
  1. the tier's own price_in / price_out (USD per 1M tokens), when both are set;
  2. LiteLLM's price table (non-streamed responses only);
  3. 0 for local tiers and for free tiers without prices;
  4. 0 with cost_src "unknown" otherwise. Such rows are visible in reports; set prices on the tier to count them.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import litellm

logger = logging.getLogger("middle_agentic.usage")

TABLE = "usage"
_GROUP_COLUMNS = {"tier": "tier_name", "key": "key_name", "job": "job", "day": "day"}


def local_day(ts: float | None = None) -> str:
    """Local calendar day (YYYY-MM-DD). Budgets reset at local midnight."""
    return time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))


def tokens_from_usage(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    else:
        prompt, completion = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
    if prompt is None or completion is None:
        return None, None
    return int(prompt), int(completion)


def price_call(
    *,
    tier_label: str,
    price_in: float | None,
    price_out: float | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    litellm_model: str,
    response: Any = None,
) -> tuple[float, str]:
    if price_in is not None and price_out is not None and prompt_tokens is not None and completion_tokens is not None:
        return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000, "tier"
    if response is not None:
        try:
            return float(litellm.completion_cost(completion_response=response)), "litellm"
        except Exception:  # noqa: BLE001 - model unknown to LiteLLM's price table
            logger.debug("no LiteLLM price for %s", litellm_model)
    if tier_label == "local":
        return 0.0, "local"
    if tier_label == "free":
        return 0.0, "free"
    return 0.0, "unknown"


class UsageLedger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        day TEXT NOT NULL,
                        key_name TEXT NOT NULL,
                        job TEXT NOT NULL,
                        tier_name TEXT NOT NULL,
                        tier_label TEXT NOT NULL,
                        model TEXT NOT NULL,
                        prompt_tokens INTEGER NOT NULL DEFAULT 0,
                        completion_tokens INTEGER NOT NULL DEFAULT 0,
                        cost_usd REAL NOT NULL DEFAULT 0,
                        cost_src TEXT NOT NULL,
                        latency_ms INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        stream INTEGER NOT NULL DEFAULT 0
                    )"""
            )
            connection.execute(f"CREATE INDEX IF NOT EXISTS idx_usage_day_key ON {TABLE} (day, key_name)")
            connection.execute(f"CREATE INDEX IF NOT EXISTS idx_usage_day_tier ON {TABLE} (day, tier_name)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def today() -> str:
        return local_day()

    def record(
        self,
        *,
        key: str,
        job: str,
        tier_name: str,
        tier_label: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_src: str,
        latency_ms: int,
        status: str,
        stream: bool,
    ) -> None:
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""INSERT INTO {TABLE} (ts, day, key_name, job, tier_name, tier_label, model, prompt_tokens,
                        completion_tokens, cost_usd, cost_src, latency_ms, status, stream)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, local_day(now), key, job, tier_name, tier_label, model, prompt_tokens,
                 completion_tokens, cost_usd, cost_src, latency_ms, status, int(stream)),
            )

    def spent_on(self, day: str, key: str | None = None) -> float:
        """Total cost for a local day, for one key or for all keys."""
        sql = f"SELECT COALESCE(SUM(cost_usd), 0) FROM {TABLE} WHERE day = ?"
        params: list[Any] = [day]
        if key is not None:
            sql += " AND key_name = ?"
            params.append(key)
        with closing(self._connect()) as connection:
            return float(connection.execute(sql, params).fetchone()[0])

    def summary(self, by: str, days: int) -> list[dict[str, Any]]:
        if by not in _GROUP_COLUMNS:
            raise ValueError(f"group by must be one of {sorted(_GROUP_COLUMNS)}")
        column = _GROUP_COLUMNS[by]
        since = local_day(time.time() - (days - 1) * 86400)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT {column} AS name, COUNT(*) AS requests,
                           SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,
                           ROUND(SUM(cost_usd), 6) AS cost_usd
                    FROM {TABLE} WHERE day >= ? AND status != 'skipped'
                    GROUP BY {column} ORDER BY cost_usd DESC, requests DESC""",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]
