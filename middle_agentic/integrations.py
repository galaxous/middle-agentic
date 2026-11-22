"""Install hook + MCP configuration for the agent tools.

Cursor, Claude Code, Codex and Antigravity get lifecycle hooks plus MCP. Continue gets MCP only
(its memory use comes from the rules written by ``magent rules install``).

Configs are merged, never overwritten: existing entries are kept, previous middle-agentic
entries are replaced, and the original file is saved as ``<file>.bak`` before writing.
Secrets are not written into these files; the hook reads the gateway key from the Keychain or the magent home .env.
The hook script is the one installed with this package, so the installed tool never depends on a source checkout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOK_SCRIPT = Path(__file__).resolve().parent / "bridge" / "magent_hook.py"
MARKER = "magent_hook.py"
MCP_SERVER_NAME = "middle-agentic"

CURSOR_EVENTS = ("sessionStart", "beforeSubmitPrompt", "afterAgentResponse")
CLAUDE_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")
CODEX_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop")
ANTIGRAVITY_EVENTS = ("SessionStart", "PreInvocation")
HOOK_TIMEOUT_SECONDS = 10
# Written as additionalContextLimit on each Codex hook; the codex adapter truncates below it.
CODEX_CONTEXT_LIMIT = 9_000


def hook_command(tool: str, url: str | None = None, event: str | None = None) -> str:
    command = f'"{sys.executable}" "{HOOK_SCRIPT}" {tool}'
    if event:
        command += f" --event {event}"  # for tools whose payload has no event name
    return f"{command} --url {url}" if url else command


def mcp_server_entry() -> dict[str, Any]:
    return {"command": sys.executable, "args": ["-m", "middle_agentic", "mcp"]}


KEY_ENV = "MAGENT_API_KEY"


def gateway_mcp_url(url: str | None = None) -> str:
    """The gateway's /mcp endpoint: the resolved gateway URL (MAGENT_URL / MAGENT_DEFAULT_NODE) or this machine."""
    base = (url or f"http://127.0.0.1:{os.environ.get('GATEWAY_PORT', '8000')}").rstrip("/")
    return f"{base}/mcp/"


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
    mcp_config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = {
        "url": gateway_mcp_url(url),
        "headers": {"Authorization": f"Bearer ${{env:{KEY_ENV}}}"},
    }
    _write_json(mcp_path, mcp_config)
    return [hooks_path, mcp_path]


def _claude_http_entry(url: str | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "url": gateway_mcp_url(url),
        "headers": {"Authorization": f"Bearer ${{{KEY_ENV}}}"},
    }


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
        mcp_config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = _claude_http_entry(url)
        _write_json(mcp_path, mcp_config)
        written.append(mcp_path)
    else:
        # User-scope MCP servers live in ~/.claude.json, which the claude CLI owns.
        entry = _claude_http_entry(url)
        command = [
            "claude", "mcp", "add", "--transport", "http", "--scope", "user", MCP_SERVER_NAME, entry["url"],
            "--header", f"Authorization: Bearer ${{{KEY_ENV}}}",
        ]
        if shutil.which("claude"):
            subprocess.run(["claude", "mcp", "remove", "--scope", "user", MCP_SERVER_NAME], capture_output=True)
            result = subprocess.run(command, capture_output=True, text=True)
            written.append("claude mcp (user scope)" if result.returncode == 0 else f"claude mcp add failed: {result.stderr.strip()}")
        else:
            # Same place `claude mcp add --scope user` writes to.
            claude_json = Path.home() / ".claude.json"
            config = _load_json(claude_json, {})
            config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = entry
            _write_json(claude_json, config)
            written.append(claude_json)
    return written


def _mcp_scope_base(scope: str, project_dir: Path | None, relative: str) -> Path:
    """Base for a file that lives at ``<project>/<relative>`` or ``~/<relative>``."""
    if scope == "project":
        if project_dir is None:
            raise ValueError("--project is required for project scope")
        return project_dir.resolve() / relative
    return Path.home() / relative


def install_antigravity(
    scope: str = "user", project_dir: Path | None = None, url: str | None = None
) -> list[Path]:
    """MCP + hooks. Global: ~/.gemini/config/ ; project: <project>/.agents/ (mcp_config.json, hooks.json)."""
    if scope == "project":
        base = _mcp_scope_base(scope, project_dir, ".agents")
    else:
        base = Path.home() / ".gemini" / "config"

    mcp_path = base / "mcp_config.json"
    mcp_config = _load_json(mcp_path, {"mcpServers": {}})
    mcp_config.setdefault("mcpServers", {})[MCP_SERVER_NAME] = mcp_server_entry()
    _write_json(mcp_path, mcp_config)

    # Antigravity hooks are a map of hook name -> {event: [handlers]}; ours is one named entry.
    hooks_path = base / "hooks.json"
    hooks_config = _load_json(hooks_path, {})
    hooks_config[MCP_SERVER_NAME] = {
        event: [
            {"type": "command", "command": hook_command("antigravity", url, event), "timeout": HOOK_TIMEOUT_SECONDS}
        ]
        for event in ANTIGRAVITY_EVENTS
    }
    _write_json(hooks_path, hooks_config)
    return [mcp_path, hooks_path]


def continue_server_yaml() -> str:
    """Standalone Continue MCP server file (``name``/``version``/``schema`` header, stdio entry)."""
    entry = mcp_server_entry()
    # JSON scalars and flow lists are valid YAML, so json.dumps gives safe quoting for paths.
    return (
        "name: middle-agentic\n"
        "version: 0.0.1\n"
        "schema: v1\n"
        "mcpServers:\n"
        f"  - name: {MCP_SERVER_NAME}\n"
        "    type: stdio\n"
        f"    command: {json.dumps(entry['command'])}\n"
        f"    args: {json.dumps(entry['args'])}\n"
    )


def install_continue(scope: str = "user", project_dir: Path | None = None) -> list[Path]:
    """MCP only. Continue loads standalone files from <project>/.continue/mcpServers/ (or ~/.continue/ globally)."""
    base = _mcp_scope_base(scope, project_dir, ".continue/mcpServers")
    path = base / f"{MCP_SERVER_NAME}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(continue_server_yaml(), encoding="utf-8")  # file is owned by middle-agentic
    return [path]


CODEX_BLOCK_START = "# >>> middle-agentic (managed) >>>"
CODEX_BLOCK_END = "# <<< middle-agentic (managed) <<<"
_CODEX_BLOCK = re.compile(re.escape(CODEX_BLOCK_START) + r".*?" + re.escape(CODEX_BLOCK_END) + r"\n?", re.DOTALL)


def codex_block(url: str | None = None) -> str:
    # Codex reads the bearer token from the named environment variable; the value never lands in the file.
    return (
        f"{CODEX_BLOCK_START}\n"
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"url = {json.dumps(gateway_mcp_url(url))}\n"
        f"bearer_token_env_var = {json.dumps(KEY_ENV)}\n"
        f"{CODEX_BLOCK_END}\n"
    )


def install_codex(scope: str = "user", project_dir: Path | None = None, url: str | None = None) -> list[Path]:
    """MCP + hooks. MCP goes into a managed block in config.toml; hooks into hooks.json.

    Codex runs non-managed hooks only after they are trusted: run `/hooks` in the CLI once after install.
    """
    path = _mcp_scope_base(scope, project_dir, ".codex/config.toml")
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    outside = _CODEX_BLOCK.sub("", existing)
    if re.search(rf"^\[mcp_servers\.{re.escape(MCP_SERVER_NAME)}\]", outside, re.MULTILINE):
        # A hand-written table with the same name would make the TOML invalid; never overwrite it silently.
        raise ValueError(f"{path} already defines [mcp_servers.{MCP_SERVER_NAME}] outside the managed block")
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    if _CODEX_BLOCK.search(existing):
        updated = _CODEX_BLOCK.sub(lambda _: codex_block(url), existing, count=1)
    else:
        updated = f"{existing.rstrip()}\n\n{codex_block(url)}" if existing.strip() else codex_block(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")

    hooks_path = _mcp_scope_base(scope, project_dir, ".codex/hooks.json")
    hooks_config = _load_json(hooks_path, {})
    hooks = hooks_config.setdefault("hooks", {})
    for event in CODEX_EVENTS:
        groups = [
            group for group in hooks.get(event, [])
            if not any(MARKER in str(hook.get("command", "")) for hook in group.get("hooks", []))
        ]
        groups.append({"hooks": [{
            "type": "command",
            "command": hook_command("codex", url),
            "timeout": HOOK_TIMEOUT_SECONDS,
            "additionalContextLimit": CODEX_CONTEXT_LIMIT,
        }]})
        hooks[event] = groups
    _write_json(hooks_path, hooks_config)
    return [path, hooks_path]
