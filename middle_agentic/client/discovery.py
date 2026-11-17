"""Node discovery: local gateway + static peers from config.yaml + Tailscale peers.

Tailscale peers are listed with ``tailscale status --json`` and probed on
``http://<tailscale-ip>:<discovery.port>/health``; only hosts answering as a middle-agentic
gateway count as nodes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from middle_agentic.config import Settings, client_api_key

TAILSCALE_CANDIDATES = (
    "tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    r"C:\Program Files\Tailscale\tailscale.exe",
)
LOCAL_ALIASES = frozenset({"local", "self", "localhost"})


@dataclass
class Node:
    name: str
    url: str
    source: str
    api_key: str = field(default="", repr=False)
    online: bool | None = None
    info: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("api_key")
        return data


def find_tailscale() -> str | None:
    for candidate in TAILSCALE_CANDIDATES:
        path = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
        if path:
            return path
    return None


def tailscale_peers(timeout: float = 5.0) -> list[dict[str, Any]]:
    """Return tailnet devices (including this one, flagged ``self``); empty list if unavailable."""
    binary = find_tailscale()
    if not binary:
        return []
    try:
        completed = subprocess.run([binary, "status", "--json"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    def convert(entry: dict[str, Any], is_self: bool) -> dict[str, Any] | None:
        ips = entry.get("TailscaleIPs") or []
        ipv4 = next((ip for ip in ips if ":" not in ip), ips[0] if ips else None)
        if not ipv4:
            return None
        dns_name = (entry.get("DNSName") or "").rstrip(".")
        short = (dns_name.split(".")[0] if dns_name else entry.get("HostName", "")).lower()
        return {
            "name": short or ipv4,
            "hostname": entry.get("HostName", ""),
            "dns_name": dns_name,
            "ip": ipv4,
            "online": bool(entry.get("Online", is_self)),
            "os": entry.get("OS", ""),
            "self": is_self,
        }

    devices = []
    if status.get("Self"):
        converted = convert(status["Self"], True)
        if converted:
            devices.append(converted)
    for entry in (status.get("Peer") or {}).values():
        converted = convert(entry, False)
        if converted:
            devices.append(converted)
    return devices


def local_node(settings: Settings) -> Node:
    return Node(
        name=settings.node.name,
        url=f"http://127.0.0.1:{settings.gateway.port}",
        source="local",
        api_key=settings.gateway.api_key or client_api_key(),
    )


def _peer_key(api_key_env: str | None) -> str:
    return (os.getenv(api_key_env, "") if api_key_env else "") or client_api_key()


def candidate_nodes(settings: Settings, include_tailscale: bool = True) -> list[Node]:
    nodes: list[Node] = []
    seen: set[str] = set()

    def add(node: Node) -> None:
        if node.name.lower() not in seen:
            seen.add(node.name.lower())
            nodes.append(node)

    if settings.has_role("gateway"):
        add(local_node(settings))
    for peer in settings.peers:
        add(Node(name=peer.name, url=peer.url.rstrip("/"), source="config", api_key=_peer_key(peer.api_key_env)))
    if include_tailscale and settings.discovery.tailscale:
        for device in tailscale_peers():
            if device["self"]:
                continue
            node = Node(
                name=device["name"],
                url=f"http://{device['ip']}:{settings.discovery.port}",
                source="tailscale",
                api_key=client_api_key(),
                info={"os": device["os"], "dns_name": device["dns_name"]},
            )
            if not device["online"]:
                node.online = False
            add(node)
    return nodes


def probe_node(node: Node, timeout: float) -> Node:
    if node.online is False:
        return node
    try:
        response = httpx.get(f"{node.url}/health", timeout=timeout)
        payload = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        payload = {}
    node.online = payload.get("service") == "middle-agentic"
    if node.online:
        node.info.update({
            "node_name": payload.get("node", {}).get("name"),
            "roles": payload.get("node", {}).get("roles", []),
            "version": payload.get("version"),
            "auth_required": payload.get("auth_required"),
            "chain": [
                {"name": tier["name"], "tier": tier["tier"], "available": tier["available"]}
                for tier in payload.get("chain", [])
            ],
        })
    return node


def discover_nodes(settings: Settings, probe: bool = True, only_online: bool = False) -> list[Node]:
    nodes = candidate_nodes(settings)
    if probe and nodes:
        with ThreadPoolExecutor(max_workers=min(16, len(nodes))) as pool:
            nodes = list(pool.map(lambda node: probe_node(node, settings.discovery.probe_timeout), nodes))
    if only_online:
        nodes = [node for node in nodes if node.online]
    return nodes


def resolve_node(name: str | None, settings: Settings) -> Node:
    """Resolve a node name, alias or URL to a reachable base URL without probing."""
    target = (name or "").strip() or os.getenv("MAGENT_URL", "") or (settings.client.default_node or "")
    if not target or target.lower() in LOCAL_ALIASES:
        return local_node(settings)
    if target.startswith(("http://", "https://")):
        return Node(name=target, url=target.rstrip("/"), source="url", api_key=client_api_key())
    if target.lower() == settings.node.name.lower():
        return local_node(settings)

    wanted = target.lower()
    for peer in settings.peers:
        if peer.name.lower() == wanted:
            return Node(name=peer.name, url=peer.url.rstrip("/"), source="config", api_key=_peer_key(peer.api_key_env))

    if settings.discovery.tailscale:
        devices = [device for device in tailscale_peers() if not device["self"]]
        exact = [d for d in devices if wanted in (d["name"], d["hostname"].lower(), d["dns_name"].lower(), d["ip"])]
        prefix = [d for d in devices if d["name"].startswith(wanted)]
        match = (exact or prefix or [None])[0]
        if match:
            return Node(
                name=match["name"],
                url=f"http://{match['ip']}:{settings.discovery.port}",
                source="tailscale",
                api_key=client_api_key(),
            )

    # Fall back to plain DNS (Tailscale MagicDNS names resolve this way too).
    return Node(name=target, url=f"http://{target}:{settings.discovery.port}", source="dns", api_key=client_api_key())
