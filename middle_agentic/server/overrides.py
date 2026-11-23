"""Runtime overrides for the model chain and the daily budget, changed without restarting the gateway.

config.yaml stays the base. Changes made through the admin API (or `magent models` / `magent budget`)
are stored in the gateway's SQLite file and applied to the next request. `version()` changes on every
write, so the router rebuilds its effective chain only when something actually changed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from middle_agentic.config import TierConfig

TIERS_TABLE = "tier_overrides"
SETTINGS_TABLE = "runtime_settings"

# Fields a tier override may change. "position" is not stored per tier; ordering lives in the "order" setting.
TIER_FIELDS = ("enabled", "model", "timeout", "price_in", "price_out", "max_concurrency", "queue_wait")
ON_EXCEED = ("downgrade", "block")


class OverrideError(ValueError):
    pass


def _validate_tier_change(field: str, value: Any) -> Any:
    if field not in TIER_FIELDS:
        raise OverrideError(f"unknown field '{field}'; allowed: {', '.join(TIER_FIELDS)}")
    if value is None:
        return None
    if field == "enabled":
        if not isinstance(value, bool):
            raise OverrideError("enabled must be true or false")
        return value
    if field == "model":
        if not isinstance(value, str) or not value.strip():
            raise OverrideError("model must be a non-empty string")
        return value.strip()
    if field == "max_concurrency":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise OverrideError("max_concurrency must be 0 (no limit) or a positive integer")
        return value or None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OverrideError(f"{field} must be a number") from exc
    if field == "timeout" and number <= 0:
        raise OverrideError("timeout must be greater than 0")
    if field in ("price_in", "price_out", "queue_wait") and number < 0:
        raise OverrideError(f"{field} must be zero or positive")
    return number


class OverrideStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {TIERS_TABLE} (
                        name TEXT PRIMARY KEY,
                        data TEXT NOT NULL,
                        updated_ts REAL NOT NULL
                    )"""
            )
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {SETTINGS_TABLE} (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_ts REAL NOT NULL
                    )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def version(self) -> tuple[int, float, int, float]:
        """Changes whenever any override or setting is written or cleared."""
        with closing(self._connect()) as connection:
            tiers = connection.execute(f"SELECT COUNT(*), COALESCE(MAX(updated_ts), 0) FROM {TIERS_TABLE}").fetchone()
            settings = connection.execute(f"SELECT COUNT(*), COALESCE(MAX(updated_ts), 0) FROM {SETTINGS_TABLE}").fetchone()
        return (tiers[0], tiers[1], settings[0], settings[1])

    # -- tiers ---------------------------------------------------------------------------
    def tier_overrides(self) -> dict[str, dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(f"SELECT name, data FROM {TIERS_TABLE}").fetchall()
        return {row["name"]: json.loads(row["data"]) for row in rows}

    def set_tier(self, name: str, changes: dict[str, Any]) -> dict[str, Any]:
        validated = {field: _validate_tier_change(field, value) for field, value in changes.items()}
        current = self.tier_overrides().get(name, {})
        for field, value in validated.items():
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
        now = time.time()
        with closing(self._connect()) as connection, connection:
            if current:
                connection.execute(
                    f"INSERT INTO {TIERS_TABLE} (name, data, updated_ts) VALUES (?, ?, ?) "
                    f"ON CONFLICT(name) DO UPDATE SET data = excluded.data, updated_ts = excluded.updated_ts",
                    (name, json.dumps(current), now),
                )
            else:
                connection.execute(f"DELETE FROM {TIERS_TABLE} WHERE name = ?", (name,))
        return current

    def clear_tier(self, name: str) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute(f"DELETE FROM {TIERS_TABLE} WHERE name = ?", (name,)).rowcount > 0

    def clear_all_tiers(self) -> int:
        with closing(self._connect()) as connection, connection:
            return connection.execute(f"DELETE FROM {TIERS_TABLE}").rowcount

    # -- settings (order, budget) --------------------------------------------------------
    def _get_setting(self, key: str) -> Any:
        with closing(self._connect()) as connection:
            row = connection.execute(f"SELECT value FROM {SETTINGS_TABLE} WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def _set_setting(self, key: str, value: Any) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"INSERT INTO {SETTINGS_TABLE} (key, value, updated_ts) VALUES (?, ?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                (key, json.dumps(value), time.time()),
            )

    def _clear_setting(self, key: str) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute(f"DELETE FROM {SETTINGS_TABLE} WHERE key = ?", (key,)).rowcount > 0

    def order(self) -> list[str]:
        return list(self._get_setting("order") or [])

    def set_order(self, names: list[str]) -> None:
        if len(set(names)) != len(names):
            raise OverrideError("a tier may appear only once in the order")
        self._set_setting("order", list(names))

    def clear_order(self) -> bool:
        return self._clear_setting("order")

    def budget(self) -> dict[str, Any]:
        return dict(self._get_setting("budget") or {})

    def set_budget(self, daily_usd: float | None = None, on_exceed: str | None = None) -> dict[str, Any]:
        current = self.budget()
        if daily_usd is not None:
            try:
                value = float(daily_usd)
            except (TypeError, ValueError) as exc:
                raise OverrideError("daily_usd must be a number") from exc
            if value < 0:
                raise OverrideError("daily_usd must be zero (no cap) or positive")
            current["daily_usd"] = value
        if on_exceed is not None:
            if on_exceed not in ON_EXCEED:
                raise OverrideError(f"on_exceed must be one of {', '.join(ON_EXCEED)}")
            current["on_exceed"] = on_exceed
        self._set_setting("budget", current)
        return current

    def clear_budget(self) -> bool:
        return self._clear_setting("budget")


def effective_configs(
    base: list[TierConfig], overrides: dict[str, dict[str, Any]], order: list[str]
) -> list[TierConfig]:
    """Base tiers with overrides applied; disabled tiers removed; listed tiers first in the given order."""
    known = {config.name for config in base}
    for name in order:
        if name not in known:
            raise OverrideError(f"order names an unknown tier '{name}'")
    by_name = {config.name: config for config in base}
    rank = {name: index for index, name in enumerate(order)}
    base_index = {config.name: index for index, config in enumerate(base)}
    names = sorted(by_name, key=lambda name: (rank.get(name, len(order)), base_index[name]))

    effective: list[TierConfig] = []
    for name in names:
        change = overrides.get(name, {})
        if change.get("enabled") is False:
            continue
        updates = {field: change[field] for field in ("model", "timeout", "price_in", "price_out", "max_concurrency", "queue_wait") if field in change}
        effective.append(by_name[name].model_copy(update=updates))
    return effective
