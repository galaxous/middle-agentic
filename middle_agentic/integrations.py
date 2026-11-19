"""Install hook + MCP configuration for Cursor and Claude Code.

Configs are merged, never overwritten: existing entries are kept, previous middle-agentic
entries are replaced, and the original file is saved as ``<file>.bak`` before writing.
Secrets are not written into these files; the hook script reads them from the repo's .env.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from middle_agentic.config import PROJECT_ROOT

HOOK_SCRIPT = PROJECT_ROOT / "integrations" / "hooks" / "magent_hook.py"
MARKER = "magent_hook.py"
MCP_SERVER_NAME = "middle-agentic"

CURSOR_EVENTS = ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")
HOOK_TIMEOUT_SECONDS = 10


def hook_command(tool: str, url: str | None = None) -> str:
    command = f'"{sys.executable}" "{HOOK_SCRIPT}" {tool}'
    return f"{command} --url {url}" if url else command


def mcp_server_entry() -> dict[str, Any]:
    return {"command": sys.executable, "args": ["-m", "middle_agentic", "mcp"]}


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _base_dir(scope: str, project_dir: Path | None, folder: str) -> Path:
    if scope == "project":
        if project_dir is None:
            raise ValueError("--project is required for project scope")
        return project_dir.resolve() / folder
    return Path.home() / folder


def install_cursor(scope: str = "user", project_dir: Path | None = None, url: str | None = None) -> list[Path]:
    base = _base_dir(scope, project_dir, ".cursor")

    hooks_path = base / "hooks.json"
    hooks_config = _load_json(hooks_path, {"version": 1, "hooks": {}})
    hooks_config.setdefault("version", 1)
    hooks = hooks_config.setdefault("hooks", {})
    for event in CURSOR_EVENTS:
        entries = [entry for entry in hooks.get(event, []) if MARKER not in str(entry.get("command", ""))]
        entries.append({"command": hook_command("cursor", url), "timeout": HOOK_TIMEOUT_SECONDS})
        hooks[event] = entries
    _write_json(hooks_path, hooks_config)

    mcp_path = base / "mcp.json"
    mcp_config = _load_json(mcp_path, {"mcpServers": {}})
    mcp_config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = mcp_server_entry()
    _write_json(mcp_path, mcp_config)
    return [hooks_path, mcp_path]


def install_claude(scope: str = "user", project_dir: Path | None = None, url: str | None = None) -> list[Path | str]:
    base = _base_dir(scope, project_dir, ".claude")

    settings_path = base / "settings.json"
    settings = _load_json(settings_path, {})
    hooks = settings.setdefault("hooks", {})
    for event in CLAUDE_EVENTS:
        groups = [
            group for group in hooks.get(event, [])
            if not any(MARKER in str(hook.get("command", "")) for hook in group.get("hooks", []))
        ]
        groups.append({"hooks": [{"type": "command", "command": hook_command("claude", url), "timeout": HOOK_TIMEOUT_SECONDS}]})
        hooks[event] = groups
    _write_json(settings_path, settings)
    written: list[Path | str] = [settings_path]

    if scope == "project":
        mcp_path = project_dir.resolve() / ".mcp.json"  # type: ignore[union-attr]
        mcp_config = _load_json(mcp_path, {"mcpServers": {}})
        mcp_config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = mcp_server_entry()
        _write_json(mcp_path, mcp_config)
        written.append(mcp_path)
    else:
        # User-scope MCP servers live in ~/.claude.json, which the claude CLI owns.
        entry = mcp_server_entry()
        command = ["claude", "mcp", "add", "--scope", "user", MCP_SERVER_NAME, "--", entry["command"], *entry["args"]]
        if shutil.which("claude"):
            subprocess.run(["claude", "mcp", "remove", "--scope", "user", MCP_SERVER_NAME], capture_output=True)
            result = subprocess.run(command, capture_output=True, text=True)
            written.append("claude mcp (user scope)" if result.returncode == 0 else f"claude mcp add failed: {result.stderr.strip()}")
        else:
            # Same place `claude mcp add --scope user` writes to.
            claude_json = Path.home() / ".claude.json"
            config = _load_json(claude_json, {})
            config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = {"type": "stdio", **entry, "env": {}}
            _write_json(claude_json, config)
            written.append(claude_json)
    return written
