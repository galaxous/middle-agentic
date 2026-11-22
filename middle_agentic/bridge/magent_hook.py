#!/usr/bin/env python3
"""Bridge agent-tool hook events to a middle-agentic gateway. Installed as middle_agentic/bridge/magent_hook.py.

Usage (wired by `magent integrate cursor|claude|codex|antigravity`):
    magent_hook.py cursor|claude|codex [--url http://node:8000] [--timeout 4]
    magent_hook.py antigravity --event PreInvocation [--url ...]

Antigravity payloads have no event name, so its hook passes --event. Other tools send it in the payload.

Reads the hook payload from stdin, POSTs it to <url>/v1/hooks/<tool>/<event> and prints the
gateway's answer on stdout in the format the tool expects.

Fail-open by design: if the gateway is down, slow or misconfigured, a neutral answer is printed
and the agent continues untouched. Standard library only, so it runs with any Python 3.8+.

Configuration (environment, or the .env file in the magent home directory), first match wins:
    --url                 set by `magent integrate` (the resolved default node)
    MAGENT_URL            gateway base URL
    MAGENT_DEFAULT_NODE   node name or URL; names become http://<name>:$MAGENT_PORT (MagicDNS)
    otherwise             http://127.0.0.1:$GATEWAY_PORT (default port 8000)
    MAGENT_API_KEY        bearer key; if unset, read from the Keychain/DPAPI item `gateway` via this python
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Everything the hook reads or runs lives outside the source checkout (see middle_agentic.config.magent_home).
HOME = Path(os.environ.get("MAGENT_HOME") or Path.home() / ".config" / "middle-agentic").expanduser()


def load_env(path):
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_base_url(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("MAGENT_URL"):
        return os.environ["MAGENT_URL"]
    node = os.environ.get("MAGENT_DEFAULT_NODE", "").strip()
    if node and node.lower() not in ("local", "localhost", "127.0.0.1"):
        if node.startswith(("http://", "https://")):
            return node
        return "http://{}:{}".format(node, os.environ.get("MAGENT_PORT", "8000"))
    return "http://127.0.0.1:{}".format(os.environ.get("GATEWAY_PORT", "8000"))


def stored_api_key():
    """Key from the token store, read by the same python that installed the hook (has middle_agentic)."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "middle_agentic", "token", "get", "gateway"],
            cwd=str(Path.home()), capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def neutral_answer(tool, event):
    return {"continue": True} if (tool, event) == ("cursor", "beforeSubmitPrompt") else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tool", choices=["cursor", "claude", "codex", "antigravity"])
    parser.add_argument("--event", default=None, help="event name, for tools whose payload has none (antigravity)")
    parser.add_argument("--url", default=None)
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("MAGENT_HOOK_TIMEOUT", "4")))
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    event = args.event or str(payload.get("hook_event_name") or "")

    answer = neutral_answer(args.tool, event)
    if event:
        load_env(HOME / ".env")
        base_url = resolve_base_url(args.url)
        api_key = os.environ.get("MAGENT_API_KEY") or stored_api_key()
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        request = urllib.request.Request(
            "{}/v1/hooks/{}/{}".format(base_url.rstrip("/"), args.tool, event),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                body = json.loads(response.read() or b"{}")
            if isinstance(body, dict):
                answer = body
        except (urllib.error.URLError, OSError, ValueError) as exc:
            sys.stderr.write("magent hook: gateway unavailable, continuing ({})\n".format(exc))

    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
