"""Move a gateway that runs from a source checkout onto the installed package and magent home.

After this, the gateway service runs the installed `magent` and reads/writes only the magent home.
The checkout is only read once, during the move, and never changed.

Steps (each is reported; nothing is overwritten unless --force):
  1. stop the old service (so the database is not written during the copy)
  2. copy agent_memory.db with the SQLite backup API (consistent even with WAL)
  3. copy .env (mode 0600; MEMORY_DB_PATH lines are dropped so the new location is used)
  4. copy config.yaml if the magent home has none
  5. install and start the service from the installed package
  6. wait for /health
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from middle_agentic.config import magent_home


class GatewaySetupError(RuntimeError):
    pass


def _backup_database(source: Path, target: Path, force: bool) -> str:
    if not source.is_file():
        return f"no database at {source}; a new one will be created"
    if target.exists() and not force:
        raise GatewaySetupError(f"{target} already exists; move it aside or pass --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    # The backup API copies a consistent snapshot, including data still in the WAL file.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(tmp) as dst:
        src.backup(dst)
    os.replace(tmp, target)
    for suffix in ("-wal", "-shm"):
        leftover = target.with_name(target.name + suffix)
        if leftover.exists():
            leftover.unlink()
    return f"database copied: {source} -> {target}"


def _copy_env(source: Path, target: Path, force: bool) -> str:
    if not source.is_file():
        return f"no .env at {source}; skipped"
    if target.exists() and not force:
        return f"{target} already exists; kept (pass --force to replace)"
    lines = [line for line in source.read_text(encoding="utf-8").splitlines() if not re.match(r"^\s*MEMORY_DB_PATH\s*=", line)]
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return f".env copied (mode 0600): {source} -> {target}"


def _copy_config(source: Path, target: Path) -> str:
    if not source.is_file():
        return f"no config.yaml at {source}; the defaults apply"
    if target.exists():
        return f"{target} already exists; kept"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return f"config.yaml copied: {source} -> {target}"


def migrate(
    repo: Path | None,
    *,
    home: Path | None = None,
    force: bool = False,
    stop_service: Callable[[], str],
) -> list[str]:
    """Copy database, .env and config from a checkout into the magent home. The checkout is not modified."""
    home = home or magent_home()
    home.mkdir(parents=True, exist_ok=True)
    messages = [stop_service()]
    if repo is None:
        messages.append(f"no checkout given; using the magent home at {home}")
        return messages
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise GatewaySetupError(f"not a directory: {repo}")
    messages.append(_backup_database(repo / "agent_memory.db", home / "agent_memory.db", force))
    messages.append(_copy_env(repo / ".env", home / ".env", force))
    messages.append(_copy_config(repo / "config.yaml", home / "config.yaml"))
    return messages


def wait_for_health(port: int, timeout: float = 30.0) -> str:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed local URL
                body = response.read().decode("utf-8", "replace")
                if '"service":"middle-agentic"' in body.replace(" ", ""):
                    return f"gateway healthy at {url}"
                last = f"unexpected answer: {body[:120]}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1)
    raise GatewaySetupError(f"gateway did not become healthy within {int(timeout)}s ({last})")
