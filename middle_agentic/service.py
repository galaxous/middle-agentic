"""Register the gateway as a persistent background service.

    Linux    systemd user unit  ~/.config/systemd/user/magent-gateway.service (+ linger)
    macOS    launchd agent      ~/Library/LaunchAgents/com.galaxous.magent-gateway.plist
    Windows  Scheduled Task     MiddleAgentGateway (at startup as SYSTEM when elevated, else at logon)
"""
from __future__ import annotations

import ctypes
import getpass
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from middle_agentic.config import PROJECT_ROOT

SERVICE_NAME = "magent-gateway"
LAUNCHD_LABEL = "com.galaxous.magent-gateway"
WINDOWS_TASK = "MiddleAgentGateway"


class ServiceError(RuntimeError):
    pass


def gateway_command() -> list[str]:
    return [sys.executable, "-m", "middle_agentic", "serve"]


def _run(command: list[str], check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        raise ServiceError(f"{' '.join(command)} failed: {(result.stderr or result.stdout).strip()}")
    return result


# ---------------------------------------------------------------------------- Linux (systemd --user)
def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_env() -> dict[str, str]:
    env = dict(os.environ)
    # `systemctl --user` needs the user bus; it is missing when invoked through sudo -u.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _linux_install() -> str:
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        "[Unit]\n"
        "Description=middle-agentic gateway\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={PROJECT_ROOT}\n"
        f"ExecStart={shlex.join(gateway_command())}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    env = _systemd_env()
    _run(["systemctl", "--user", "daemon-reload"], env=env)
    _run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"], env=env)

    # Linger keeps user services running without an active login session (headless workers).
    user = getpass.getuser()
    linger = _run(["loginctl", "enable-linger", user], check=False)
    note = "" if linger.returncode == 0 else f"\nRun once to start at boot without login: sudo loginctl enable-linger {user}"
    return f"systemd user unit installed: {unit_path}\nLogs: journalctl --user -u {SERVICE_NAME} -f{note}"


def _linux_uninstall() -> str:
    env = _systemd_env()
    _run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"], check=False, env=env)
    _systemd_unit_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"], check=False, env=env)
    return "systemd user unit removed"


def _linux_status() -> str:
    result = _run(["systemctl", "--user", "is-active", f"{SERVICE_NAME}.service"], check=False, env=_systemd_env())
    return f"systemd: {result.stdout.strip() or result.stderr.strip() or 'unknown'}"


# ---------------------------------------------------------------------------- macOS (launchd)
def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _macos_install() -> str:
    plist_path = _plist_path()
    log_path = Path.home() / "Library" / "Logs" / f"{SERVICE_NAME}.log"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": gateway_command(),
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        # launchd starts with a minimal PATH; tailscale and ollama live in Homebrew.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)

    _run(["launchctl", "bootout", _launchd_domain(), str(plist_path)], check=False)
    _run(["launchctl", "bootstrap", _launchd_domain(), str(plist_path)])
    _run(["launchctl", "kickstart", "-k", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False)
    return f"launchd agent installed: {plist_path}\nLogs: {log_path}"


def _macos_uninstall() -> str:
    plist_path = _plist_path()
    _run(["launchctl", "bootout", _launchd_domain(), str(plist_path)], check=False)
    plist_path.unlink(missing_ok=True)
    return "launchd agent removed"


def _macos_status() -> str:
    result = _run(["launchctl", "print", f"{_launchd_domain()}/{LAUNCHD_LABEL}"], check=False)
    if result.returncode != 0:
        return "launchd: not installed"
    state = next((line.strip() for line in result.stdout.splitlines() if line.strip().startswith("state =")), "state = unknown")
    return f"launchd: {state}"


# ---------------------------------------------------------------------------- Windows (Task Scheduler)
def _is_windows_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=check)


def _windows_install() -> str:
    elevated = _is_windows_admin()
    # Task Scheduler kills tasks after 72h by default; ExecutionTimeLimit 0 keeps the gateway alive.
    trigger = "New-ScheduledTaskTrigger -AtStartup" if elevated else "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME"
    principal = (
        "New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest"
        if elevated
        else "New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive"
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {_ps_quote(sys.executable)} -Argument '-m middle_agentic serve' -WorkingDirectory {_ps_quote(str(PROJECT_ROOT))}
$trigger = {trigger}
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = {principal}
Register-ScheduledTask -TaskName '{WINDOWS_TASK}' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName '{WINDOWS_TASK}'
"""
    _powershell(script)
    mode = "at startup as SYSTEM" if elevated else "at logon of the current user (run elevated for start-at-boot)"
    return f"Scheduled Task '{WINDOWS_TASK}' registered {mode}"


def _windows_uninstall() -> str:
    _powershell(f"Unregister-ScheduledTask -TaskName '{WINDOWS_TASK}' -Confirm:$false", check=False)
    return f"Scheduled Task '{WINDOWS_TASK}' removed"


def _windows_status() -> str:
    result = _powershell(f"(Get-ScheduledTask -TaskName '{WINDOWS_TASK}').State", check=False)
    return f"Scheduled Task: {result.stdout.strip() or 'not installed'}"


# ---------------------------------------------------------------------------- dispatch
_HANDLERS = {
    "Linux": (_linux_install, _linux_uninstall, _linux_status),
    "Darwin": (_macos_install, _macos_uninstall, _macos_status),
    "Windows": (_windows_install, _windows_uninstall, _windows_status),
}


def _handlers():
    system = platform.system()
    if system not in _HANDLERS:
        raise ServiceError(f"unsupported platform: {system}")
    return _HANDLERS[system]


def install() -> str:
    return _handlers()[0]()


def uninstall() -> str:
    return _handlers()[1]()


def status() -> str:
    return _handlers()[2]()
