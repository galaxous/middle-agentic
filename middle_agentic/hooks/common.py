"""Shared pieces for agent-tool hook adapters."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from middle_agentic.memory.graph import GraphMemory


@dataclass
class HookDeps:
    memory: GraphMemory
    enabled: bool = True
    snippet_chars: int = 500


def project_session_id(path: str | None) -> str:
    """Memory scope for a workspace, shared by Cursor, Claude Code and the MCP server.

    Path separators of both OSes are handled because the hook may run on a Windows client
    while the gateway runs on Linux (or vice versa).
    """
    if not path:
        return "default"
    normalized = path.rstrip("/\\")
    name = re.split(r"[\\/]", normalized)[-1] or "root"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name)[:48]
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    return f"project:{safe_name}-{digest}"
