"""Interactive client setup: gateway URL and key in, verified against the gateway, then installed.

Order matters: every check runs before anything is written. If a check fails, nothing on this
machine changes. Once the user confirms, the key goes to the OS token store, the gateway address
goes to the magent home .env, and the agent tools are integrated.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from middle_agentic import tokens
from middle_agentic.config import magent_home

DEFAULT_PORT = 8000
MCP_PROTOCOL = "2025-06-18"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def normalise_url(raw: str) -> str:
    """'100.68.92.41' -> 'http://100.68.92.41:8000'. Keeps an explicit scheme and port."""
    value = raw.strip().rstrip("/")
    if not value:
        raise ValueError("gateway address is empty")
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"not a valid http(s) address: {raw}")
    if parsed.port is None:
        value = f"{parsed.scheme}://{parsed.hostname}:{DEFAULT_PORT}"
    return value


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def run_checks(url: str, key: str, client: httpx.Client) -> list[Check]:
    checks: list[Check] = []
    try:
        health = client.get(f"{url}/health")
    except httpx.HTTPError as exc:
        return [Check("gateway reachable", False, f"no answer from {url}: {type(exc).__name__}")]
    body: dict[str, Any] = {}
    try:
        body = health.json()
    except ValueError:
        pass
    if health.status_code == 200 and body.get("service") == "middle-agentic":
        checks.append(Check("gateway reachable", True, f"node {body.get('node', {}).get('name', '?')}, version {body.get('version', '?')}"))
    else:
        checks.append(Check("gateway reachable", False, f"{url}/health answered {health.status_code}; not a middle-agentic gateway"))
        return checks

    mcp = client.post(
        f"{url}/mcp/",
        headers={**_bearer(key), "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": MCP_PROTOCOL, "capabilities": {}, "clientInfo": {"name": "magent-setup", "version": "0"}}},
    )
    if mcp.status_code == 200:
        checks.append(Check("key accepted by the gateway", True, "MCP endpoint answered"))
    elif mcp.status_code == 401:
        checks.append(Check("key accepted by the gateway", False, "the gateway rejected this key (unknown, revoked or expired)"))
        return checks
    else:
        checks.append(Check("key accepted by the gateway", False, f"MCP endpoint answered {mcp.status_code}"))
        return checks

    context = client.post(f"{url}/v1/memory/context", headers=_bearer(key), json={"query": "magent setup check"})
    if context.status_code == 200:
        checks.append(Check("memory read permission", True, "the key can read memory"))
    elif context.status_code == 403:
        checks.append(Check("memory read permission", False, "the key lacks memory:read; ask the gateway admin to add it"))
    else:
        checks.append(Check("memory read permission", False, f"memory endpoint answered {context.status_code}"))
    return checks


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set KEY=value in a .env file, replacing an existing line and keeping everything else."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    updated = [f"{key}={value}" if pattern.match(line) else line for line in lines]
    if not any(pattern.match(line) for line in lines):
        updated.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _confirmed(answer: str) -> bool:
    return answer.strip().lower() in ("e", "evet", "y", "yes")


def run(
    url: str | None,
    key: str | None,
    *,
    assume_yes: bool,
    ask: Callable[[str], str],
    secret: Callable[[str], str],
    client: httpx.Client,
    store: Any,
    integrate: Callable[[str], int],
    login_env: Callable[[], None] | None,
    out: Callable[[str], None] = print,
) -> int:
    try:
        target = normalise_url(url or ask(f"Gateway adresi (örn. 100.68.92.41 veya http://host:{DEFAULT_PORT}): "))
    except ValueError as exc:
        out(f"hata: {exc}")
        return 1
    api_key = (key or secret("Gateway API anahtarı (gizli, ekranda görünmez): ")).strip()
    if not api_key:
        out("hata: anahtar boş")
        return 1

    out(f"\nDoğrulanıyor: {target}")
    checks = run_checks(target, api_key, client)
    for check in checks:
        out(f"  {'✓' if check.ok else '✗'} {check.name}: {check.detail}")
    if not all(check.ok for check in checks):
        out("\nKurulum yapılmadı. Bu makinede hiçbir şey değişmedi. Adresi ve anahtarı kontrol edip tekrar deneyin.")
        return 1

    if not assume_yes:
        answer = ask("\nBağlantı doğrulandı. Kurulum bu makineye yazılsın mı? [e/H]: ")
        if not _confirmed(answer):
            out("Kurulum iptal edildi. Hiçbir şey yazılmadı.")
            return 0

    store.set("gateway", api_key)
    backend = store.backend() if hasattr(store, "backend") else type(store).__name__
    set_env_value(magent_home() / ".env", "MAGENT_URL", target)
    out(f"\n✓ anahtar kaydedildi ({backend})")
    out(f"✓ adres kaydedildi: {magent_home() / '.env'}")
    status = integrate(target)
    if login_env is not None:
        login_env()
    out("\nBitti. Açık uygulamaları (Cursor, VS Code, Claude Code) kapatıp yeniden açın.")
    out("Codex kullanıyorsanız CLI'da /hooks ile middle-agentic hook'larını bir kez güvenin.")
    return status


def read_key_from_stdin(stream) -> str:
    return stream.read().strip()
