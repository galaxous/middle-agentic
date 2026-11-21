"""`magent` command line interface.

    magent setup --roles gateway,llm,client [--model qwen2.5:3b]
    magent serve
    magent llm pull qwen2.5:3b | list | status
    magent nodes
    magent ask "..." [--node acer] [--model local] [--stream]
    magent hook context|record|forget|sessions|graph ...
    magent mcp
    magent service install|uninstall|status
    magent integrate cursor|claude|codex|antigravity [--scope user|project] [--url URL]
    magent integrate continue [--scope user|project]   (MCP only)
    magent rules install|remove|show [--tool cursor|claude|codex|antigravity|continue|all] [--project DIR]
    magent token set|get|delete <provider> | list | env     (API keys in the macOS Keychain)
    magent health [--node acer]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from middle_agentic import __version__
from middle_agentic.config import PROJECT_ROOT, ROLES, find_config_file, load_settings

EXIT_ERROR = 1


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _default_session() -> str:
    from middle_agentic.hooks.common import project_session_id

    return os.getenv("MAGENT_SESSION") or project_session_id(os.getcwd())


def _read_prompt(parts: list[str]) -> str:
    prompt = " ".join(parts).strip()
    if not prompt or prompt == "-":
        prompt = sys.stdin.read().strip()
    return prompt


# ---------------------------------------------------------------------------- setup
def _set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env_file_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""


def cmd_setup(args: argparse.Namespace) -> int:
    config_path = find_config_file() or PROJECT_ROOT / "config.yaml"
    data: dict[str, Any] = {}
    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    node = data.setdefault("node", {})
    if args.roles:
        roles = [role.strip() for role in args.roles.split(",") if role.strip()]
        unknown = sorted(set(roles) - set(ROLES))
        if unknown:
            return _fail(f"unknown roles {unknown}, allowed: {list(ROLES)}")
        node["roles"] = roles
    node.setdefault("roles", list(ROLES))
    if args.name:
        node["name"] = args.name

    llm = data.setdefault("llm", {})
    if args.runtime:
        llm["runtime"] = args.runtime
    if args.model:
        llm["model"] = args.model
        for tier in data.get("chain") or []:
            if tier.get("name") == "local":
                tier["model"] = args.model

    gateway = data.setdefault("gateway", {})
    is_gateway = "gateway" in node["roles"]
    if args.host:
        gateway["host"] = args.host
    elif is_gateway and "host" not in gateway:
        # Gateways serve other tailnet devices, so they listen on all interfaces (with an API key).
        gateway["host"] = "0.0.0.0"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"config written: {config_path}")

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file() and (PROJECT_ROOT / ".env.example").is_file():
        shutil.copy2(PROJECT_ROOT / ".env.example", env_path)
        print(f".env created from .env.example: {env_path}")

    if is_gateway and gateway.get("host") not in ("127.0.0.1", "localhost") and not _env_file_value(env_path, "GATEWAY_API_KEY"):
        key = secrets.token_urlsafe(32)
        _set_env_value(env_path, "GATEWAY_API_KEY", key)
        print("generated GATEWAY_API_KEY in .env; set the same value as MAGENT_API_KEY on client devices:")
        print(f"  {key}")

    settings = load_settings(config_path)
    if settings.has_role("llm") and settings.llm.runtime == "ollama" and not args.no_pull:
        from middle_agentic import llm as llm_module

        if llm_module.status(settings)["reachable"]:
            print(f"pulling {settings.llm.model} via ollama...")
            try:
                llm_module.pull(settings, settings.llm.model, on_progress=lambda text: print(f"  {text}"))
            except llm_module.LLMError as exc:
                print(f"warning: {exc}", file=sys.stderr)
        else:
            print(f"ollama is not running; install/start it, then run: magent llm pull {settings.llm.model}")

    print(f"\nnode '{settings.node.name}' roles={settings.node.roles}")
    if settings.has_role("gateway"):
        print("next: magent serve   (or: magent service install)")
    return 0


# ---------------------------------------------------------------------------- serve / mcp / service
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from middle_agentic.server.app import create_app

    settings = load_settings()
    if args.host:
        settings.gateway.host = args.host
    if args.port:
        settings.gateway.port = args.port
    uvicorn.run(
        create_app(settings),
        host=settings.gateway.host,
        port=settings.gateway.port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    from middle_agentic.mcp_server import run

    run()
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from middle_agentic import service

    try:
        action = {"install": service.install, "uninstall": service.uninstall, "status": service.status}[args.action]
        print(action())
    except service.ServiceError as exc:
        return _fail(str(exc))
    if args.action == "status":
        settings = load_settings()
        try:
            healthy = httpx.get(f"http://127.0.0.1:{settings.gateway.port}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            healthy = False
        state = "up" if healthy else "not responding (allow ~15s after start)"
        print(f"gateway http://127.0.0.1:{settings.gateway.port}/health: {state}")
    return 0


def _default_hook_url() -> str | None:
    """URL of MAGENT_URL / MAGENT_DEFAULT_NODE, resolved now so hooks need no discovery at runtime."""
    from middle_agentic.client.discovery import resolve_node

    node = resolve_node(None, load_settings())
    return None if node.source == "local" else node.url


def cmd_integrate(args: argparse.Namespace) -> int:
    from middle_agentic import integrations

    project = Path(args.project) if args.project else (Path.cwd() if args.scope == "project" else None)
    if args.tool == "continue":
        # MCP only: Continue has no lifecycle hooks in this integration.
        for written in integrations.install_continue(scope=args.scope, project_dir=project):
            print(f"updated: {written}")
        print("MCP only: memory use comes from the rules (magent rules install --tool continue)")
        print("restart the tool (or reload the window) to pick up the changes")
        return 0

    url = args.url or _default_hook_url()
    installers = {
        "cursor": integrations.install_cursor,
        "claude": integrations.install_claude,
        "codex": integrations.install_codex,
        "antigravity": integrations.install_antigravity,
    }
    for written in installers[args.tool](scope=args.scope, project_dir=project, url=url):
        print(f"updated: {written}")
    print(f"hooks send events to: {url or 'the local gateway (127.0.0.1)'}")
    print("add memory rules to a project with: magent rules install --project <dir>")
    if args.tool == "codex":
        print("Codex: open /hooks in the CLI once and trust the middle-agentic hooks (untrusted hooks are skipped)")
    print("restart the tool (or reload the window) to pick up the changes")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    from middle_agentic import tokens

    if args.action == "install-shell" and args.provider:
        return _fail("install-shell takes no provider; it adds lines for all providers")
    if args.action in ("set", "get", "delete") and not args.provider:
        return _fail(f"token {args.action} needs a provider: {', '.join(tokens.PROVIDERS)}")
    try:
        if args.action == "install-shell":
            shell = args.shell or ("zsh" if sys.platform == "darwin" else "powershell")
            target = Path(args.profile).expanduser() if args.profile else tokens.default_profile(shell)
            for line in tokens.install_shell(target, shell, dry_run=args.dry_run):
                print(line)
            return 0
        if args.action == "env":
            print(tokens.shell_exports(args.shell or "zsh"))
            return 0
        store = tokens.default_store()
        if args.action == "set":
            value = sys.stdin.read() if args.stdin else None
            print(tokens.set_token(store, args.provider, value))
        elif args.action == "get":
            # Bare value on stdout so it works as `apiKeyHelper` and inside $(...).
            print(tokens.get_token(store, args.provider))
        elif args.action == "delete":
            print(tokens.delete_token(store, args.provider))
        elif args.action == "list":
            print("\n".join(tokens.list_tokens(store)))
    except tokens.TokenError as exc:
        return _fail(str(exc))
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    from middle_agentic import rules

    project = Path(args.project) if args.project else Path.cwd()
    tools = rules.TOOLS if args.tool == "all" else (args.tool,)
    if args.action == "show":
        project_path = os.path.abspath(project)
        renderers = {"cursor": rules.cursor_rule, "continue": rules.continue_rule}
        render = renderers.get(args.tool, rules.memory_instructions)
        print(render(project_path))
        return 0
    if args.action == "install":
        for path in rules.install(project, tools):
            print(f"written: {path}")
        return 0
    removed = rules.remove(project, tools)
    for path in removed:
        print(f"removed: {path}")
    if not removed:
        print("nothing to remove")
    return 0


# ---------------------------------------------------------------------------- llm
def cmd_llm(args: argparse.Namespace) -> int:
    from middle_agentic import llm as llm_module

    settings = load_settings()
    try:
        if args.action == "status":
            _print_json(llm_module.status(settings))
        elif args.action == "list":
            for model in llm_module.list_models(settings):
                print(f"{model['name']:<40} {model['size_gb']:>6} GB")
        elif args.action == "pull":
            model = args.model or settings.llm.model
            llm_module.pull(settings, model, on_progress=lambda text: print(f"  {text}"))
            print(f"pulled {model}")
    except llm_module.LLMError as exc:
        return _fail(str(exc))
    return 0


# ---------------------------------------------------------------------------- client commands
def _client(args: argparse.Namespace):
    from middle_agentic.client.sdk import MiddleAgentClient

    return MiddleAgentClient(node=args.node, session_id=getattr(args, "session", None) or _default_session())


def _run_client_command(func) -> int:
    from middle_agentic.client.sdk import GatewayError

    try:
        return func()
    except GatewayError as exc:
        return _fail(f"gateway returned {exc.status_code}: {json.dumps(exc.detail, default=str)}")
    except httpx.HTTPError as exc:
        return _fail(f"could not reach node: {type(exc).__name__}: {exc}")


def cmd_nodes(args: argparse.Namespace) -> int:
    from middle_agentic.client.discovery import discover_nodes

    nodes = discover_nodes(load_settings(), only_online=not args.all)
    if args.json:
        _print_json([node.as_dict() for node in nodes])
        return 0
    if not nodes:
        print("no online nodes found (use --all to include offline/unprobed hosts)")
        return 0
    print(f"{'NAME':<20} {'SOURCE':<10} {'ONLINE':<7} {'ROLES':<22} URL")
    for node in nodes:
        roles = ",".join(node.info.get("roles", [])) or "-"
        online = {True: "yes", False: "no", None: "?"}[node.online]
        print(f"{node.name:<20} {node.source:<10} {online:<7} {roles:<22} {node.url}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    def run() -> int:
        with _client(args) as client:
            _print_json(client.health())
        return 0

    return _run_client_command(run)


def cmd_ask(args: argparse.Namespace) -> int:
    prompt = _read_prompt(args.prompt)
    if not prompt:
        return _fail("empty prompt")

    def run() -> int:
        with _client(args) as client:
            options = {"model": args.model, "system": args.system, "memory": not args.no_memory}
            if args.stream:
                for delta in client.stream(prompt, **options):
                    print(delta, end="", flush=True)
                print()
                headers = client.last_headers
                meta = (headers.get("x-gateway-node", ""), headers.get("x-gateway-tier-name", ""), headers.get("x-gateway-model", ""))
            else:
                result = client.ask(prompt, **options)
                if args.json:
                    _print_json(result.raw)
                    return 0
                print(result.text, flush=True)
                meta = (result.node, result.tier_name, result.model)
        if not args.quiet:
            print(f"[node={meta[0]} tier={meta[1]} model={meta[2]}]", file=sys.stderr)
        return 0

    return _run_client_command(run)


def cmd_hook(args: argparse.Namespace) -> int:
    def run() -> int:
        with _client(args) as client:
            if args.action == "context":
                result = client.context(" ".join(args.text), recent_limit=args.limit, related_limit=args.limit)
                if args.json:
                    _print_json(result)
                else:
                    print(result.get("context") or "(no matching memory)")
            elif args.action == "record":
                content = _read_prompt(args.text)
                if not content:
                    return _fail("empty content")
                _print_json(client.record_event(content, role=args.role or "note", metadata={"source": "cli"}))
            elif args.action == "forget":
                match = " ".join(args.text).strip() or None
                if not match and not args.id:
                    return _fail("give a text to match or --id")
                result = client.forget(
                    match,
                    ids=args.id,
                    role=args.role,
                    with_answers=not args.no_answers,
                    dry_run=not args.yes,
                )
                if args.json:
                    _print_json(result)
                    return 0
                for node in result["nodes"]:
                    snippet = " ".join(node["content"].split())[:100]
                    print(f"{node['id']}  [{node['role']}] {snippet}")
                count = len(result["nodes"])
                if result["deleted"]:
                    print(f"deleted {count} node(s) from {result['session_id']}")
                else:
                    print(f"{count} node(s) would be deleted from {result['session_id']}; re-run with --yes to delete")
            elif args.action == "sessions":
                _print_json(client.sessions())
            elif args.action == "graph":
                _print_json(client.session_graph(args.text[0] if args.text else None))
        return 0

    return _run_client_command(run)


# ---------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magent", description="middle-agentic node: gateway, local LLM and client tools")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="write config.yaml/.env for this node and pull the local model")
    setup.add_argument("--roles", help=f"comma separated, any of: {','.join(ROLES)}")
    setup.add_argument("--name", help="node name (default: hostname)")
    setup.add_argument("--runtime", choices=["ollama", "llamacpp", "vllm", "lmstudio"])
    setup.add_argument("--model", help="local model, e.g. qwen2.5:3b")
    setup.add_argument("--host", help="gateway listen address (default 0.0.0.0 for gateway nodes)")
    setup.add_argument("--no-pull", action="store_true", help="do not pull the local model")
    setup.set_defaults(func=cmd_setup)

    serve = sub.add_parser("serve", help="run the gateway in the foreground")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)

    llm = sub.add_parser("llm", help="manage the local model server")
    llm.add_argument("action", choices=["pull", "list", "status"])
    llm.add_argument("model", nargs="?")
    llm.set_defaults(func=cmd_llm)

    nodes = sub.add_parser("nodes", help="discover nodes (local, config peers, Tailscale)")
    nodes.add_argument("--all", action="store_true", help="include offline and non-gateway hosts")
    nodes.add_argument("--json", action="store_true")
    nodes.set_defaults(func=cmd_nodes)

    def add_target(command: argparse.ArgumentParser) -> None:
        command.add_argument("--node", help="node name, Tailscale host, URL or 'local' (default: client.default_node)")
        command.add_argument("--session", help="memory session (default: current project)")

    health = sub.add_parser("health", help="show a node's health and chain status")
    add_target(health)
    health.set_defaults(func=cmd_health)

    ask = sub.add_parser("ask", help="send a prompt to a node")
    ask.add_argument("prompt", nargs="*", help="prompt text, or '-' / empty to read stdin")
    add_target(ask)
    ask.add_argument("--model", default="auto", help="auto, a tier name (local, luna, ...) or label (free|local|paid)")
    ask.add_argument("--system", help="system prompt")
    ask.add_argument("--stream", action="store_true")
    ask.add_argument("--no-memory", action="store_true", help="skip memory pre/post hooks")
    ask.add_argument("--json", action="store_true", help="print the raw OpenAI response")
    ask.add_argument("--quiet", "-q", action="store_true", help="do not print node/tier info")
    ask.set_defaults(func=cmd_ask)

    hook = sub.add_parser("hook", help="memory pre/post hook primitives on a node")
    hook.add_argument("action", choices=["context", "record", "forget", "sessions", "graph"])
    hook.add_argument(
        "text", nargs="*", help="query (context), content (record), text to match (forget) or session id (graph)"
    )
    add_target(hook)
    hook.add_argument(
        "--role", default=None, help="node role: for record (default note) or a filter for forget (decision, fact, ...)"
    )
    hook.add_argument("--id", action="append", default=[], help="forget: node id (repeatable)")
    hook.add_argument("--no-answers", action="store_true", help="forget: keep answers of matched questions")
    hook.add_argument("--yes", action="store_true", help="forget: really delete (default lists matches only)")
    hook.add_argument("--limit", type=int, default=None)
    hook.add_argument("--json", action="store_true")
    hook.set_defaults(func=cmd_hook)

    mcp = sub.add_parser("mcp", help="run the MCP server on stdio")
    mcp.set_defaults(func=cmd_mcp)

    service = sub.add_parser("service", help="run the gateway as a background service")
    service.add_argument("action", choices=["install", "uninstall", "status"])
    service.set_defaults(func=cmd_service)

    integrate = sub.add_parser("integrate", help="install hooks + MCP config for Cursor or Claude Code")
    integrate.add_argument("tool", choices=["cursor", "claude", "continue", "codex", "antigravity"])
    integrate.add_argument("--scope", choices=["user", "project"], default="user")
    integrate.add_argument("--project", help="project directory for --scope project (default: cwd)")
    integrate.add_argument(
        "--url", help="gateway URL the hooks talk to (default: MAGENT_URL, then MAGENT_DEFAULT_NODE, then local)"
    )
    integrate.set_defaults(func=cmd_integrate)

    rules_parser = sub.add_parser("rules", help="write project rules that make Cursor/Claude use the memory tools")
    rules_parser.add_argument("action", choices=["install", "remove", "show"])
    rules_parser.add_argument(
        "--tool", choices=["cursor", "claude", "codex", "antigravity", "continue", "all"], default="all"
    )
    rules_parser.add_argument("--project", help="project directory (default: cwd)")
    rules_parser.set_defaults(func=cmd_rules)

    from middle_agentic.tokens import PROVIDERS

    token = sub.add_parser("token", help="store and read API tokens in the macOS Keychain")
    token.add_argument("action", choices=["set", "get", "delete", "list", "env", "install-shell"])
    token.add_argument("--shell", choices=["zsh", "bash", "powershell"], help="install-shell/env: shell (default: zsh on macOS, powershell on Windows)")
    token.add_argument("--profile", help="install-shell: profile file to update (default depends on --shell)")
    token.add_argument("--dry-run", action="store_true", help="install-shell: show what would change, write nothing")
    token.add_argument("provider", nargs="?", choices=sorted(PROVIDERS), help="anthropic, openai, gemini, openrouter")
    token.add_argument("--stdin", action="store_true", help="set: read the value from stdin instead of a prompt")
    token.set_defaults(func=cmd_token)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
