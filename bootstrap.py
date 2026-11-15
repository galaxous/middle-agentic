#!/usr/bin/env python3
"""Cross-platform entry point for the middle-agent bootstrap.

Detects the host OS and delegates to the matching platform script:

    Windows       -> bootstrap-windows.ps1  (relaunched elevated through UAC when needed)
    Linux/Darwin  -> bootstrap-unix.sh      (uses sudo internally for privileged steps)

Optionally (``--git``) initialises the local git repository, creates the initial commit and,
when an authenticated GitHub CLI is available, publishes the repository with ``gh repo create``.

Only the Python standard library is used so this file runs before any dependency is installed.

Examples:
    python3 bootstrap.py --pubkey "ssh-ed25519 AAAA... me@macbook"
    python bootstrap.py --pubkey-file C:\\keys\\mac.pub --user sshuser --shell GitBash
    python3 bootstrap.py --skip-bootstrap --git
"""
from __future__ import annotations

import argparse
import ctypes
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
WINDOWS_SCRIPT = PROJECT_ROOT / "bootstrap-windows.ps1"
UNIX_SCRIPT = PROJECT_ROOT / "bootstrap-unix.sh"
DEFAULT_REPO = "galaxous/middle-agentic"
COMMIT_MESSAGE = "chore: initial bootstrap & middle-agent gateway setup"


def info(message: str) -> None:
    print(f"==> {message}", flush=True)


def warn(message: str) -> None:
    print(f"    [WARN] {message}", file=sys.stderr, flush=True)


def fail(message: str, code: int = 1) -> None:
    print(f"    [ERROR] {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a middle-agent node on Windows, Linux or macOS.",
    )
    key_group = parser.add_mutually_exclusive_group()
    key_group.add_argument("--pubkey", help="OpenSSH public key line to authorise.")
    key_group.add_argument("--pubkey-file", type=Path, help="Path to a .pub file to authorise.")
    parser.add_argument("--user", default="sshuser", help="Windows only: local SSH account (default: sshuser).")
    parser.add_argument(
        "--shell",
        choices=["PowerShell", "GitBash", "None"],
        default="PowerShell",
        help="Windows only: OpenSSH DefaultShell (default: PowerShell).",
    )
    parser.add_argument("--skip-packages", action="store_true", help="Unix only: skip system package installation.")
    parser.add_argument("--skip-venv", action="store_true", help="Skip Python venv creation.")
    parser.add_argument("--skip-bootstrap", action="store_true", help="Do not run the platform bootstrap script.")
    parser.add_argument("--git", action="store_true", help="git init + initial commit (+ gh repo create if possible).")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repository for gh (default: {DEFAULT_REPO}).")
    parser.add_argument("--no-push", action="store_true", help="With --git: commit locally only.")
    return parser.parse_args()


def resolve_public_key(args: argparse.Namespace) -> str | None:
    if args.pubkey_file:
        try:
            key = args.pubkey_file.expanduser().read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            fail(f"Cannot read --pubkey-file: {exc}")
    else:
        key = (args.pubkey or "").strip()

    if not key:
        return None
    if '"' in key or "\n" in key:
        fail("The public key must be a single line without double quotes.")
    return key


# ---------------------------------------------------------------------------- Windows
def is_windows_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def find_powershell() -> str:
    # Windows PowerShell 5.1 is always present; pwsh is only a fallback.
    for candidate in ("powershell.exe", "pwsh.exe"):
        path = shutil.which(candidate)
        if path:
            return path
    fail("PowerShell executable not found.")
    return ""  # unreachable, keeps type checkers happy


def run_windows(args: argparse.Namespace, public_key: str | None) -> int:
    if not public_key:
        fail("Windows bootstrap requires --pubkey or --pubkey-file (the key of the machine that will connect).")

    powershell = find_powershell()
    script_args = ["-PublicKey", public_key, "-UserName", args.user, "-DefaultShell", args.shell]

    if is_windows_admin():
        info(f"Running {WINDOWS_SCRIPT.name} (elevated)")
        command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WINDOWS_SCRIPT), *script_args]
        return subprocess.run(command, cwd=PROJECT_ROOT).returncode

    # Not elevated: relaunch through UAC. -NoExit keeps the window open so the summary stays readable.
    info("Administrator rights required, requesting elevation through UAC")
    parameters = subprocess.list2cmdline(
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", str(WINDOWS_SCRIPT), *script_args]
    )
    result = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None, "runas", powershell, parameters, str(PROJECT_ROOT), 1
    )
    if result <= 32:
        fail(f"Elevation was cancelled or failed (ShellExecuteW code {result}).")
    info("Bootstrap continues in the elevated PowerShell window.")
    return 0


def setup_windows_venv() -> None:
    requirements = PROJECT_ROOT / "requirements.txt"
    if not requirements.exists():
        warn("requirements.txt not found, skipping venv.")
        return

    venv_dir = PROJECT_ROOT / "venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    info(f"Preparing Python virtual environment in {venv_dir}")
    if not venv_python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)

    pip = [str(venv_python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    subprocess.run([*pip, "--upgrade", "pip"], check=True)
    subprocess.run([*pip, "-r", str(requirements)], check=True)
    info("requirements.txt installed")


# ---------------------------------------------------------------------------- Unix
def run_unix(args: argparse.Namespace, public_key: str | None) -> int:
    bash = shutil.which("bash")
    if not bash:
        fail("bash not found on PATH.")

    command = [bash, str(UNIX_SCRIPT), "--project-dir", str(PROJECT_ROOT)]
    if public_key:
        command += ["--pubkey", public_key]
    if args.skip_packages:
        command.append("--skip-packages")
    if args.skip_venv:
        command.append("--skip-venv")

    info(f"Running {UNIX_SCRIPT.name}")
    return subprocess.run(command, cwd=PROJECT_ROOT).returncode


# ---------------------------------------------------------------------------- git / GitHub
def git(*git_args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *git_args],
        cwd=PROJECT_ROOT,
        check=check,
        text=True,
        capture_output=capture,
    )


def gh_is_authenticated() -> bool:
    if not shutil.which("gh"):
        return False
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return result.returncode == 0


def setup_git(repo: str, push: bool) -> int:
    if not shutil.which("git"):
        warn("git is not installed; skipping repository setup.")
        return 1

    # Check for .git in this folder explicitly: `git rev-parse` would also succeed inside a parent
    # repository and the commit would land there instead of in this project.
    if not (PROJECT_ROOT / ".git").exists():
        info("Initialising git repository (branch: main)")
        if git("init", "-b", "main", check=False, capture=True).returncode != 0:
            git("init")
            git("symbolic-ref", "HEAD", "refs/heads/main")
    else:
        info("git repository already initialised")

    git("add", ".")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        info("Nothing new to commit")
    else:
        commit = git("commit", "-m", COMMIT_MESSAGE, check=False, capture=True)
        if commit.returncode != 0:
            warn(commit.stderr.strip() or commit.stdout.strip())
            warn('Configure your identity: git config --global user.name "..." && git config --global user.email "..."')
            return commit.returncode
        info(f'Committed: "{COMMIT_MESSAGE}"')

    if not push:
        return 0

    create_command = ["gh", "repo", "create", repo, "--public", "--source", ".", "--remote", "origin", "--push"]
    if not gh_is_authenticated():
        info("GitHub CLI not installed or not authenticated. To publish manually run:")
        print("    gh auth login -h github.com")
        print(f"    {' '.join(create_command)}")
        return 0

    has_origin = git("remote", "get-url", "origin", check=False, capture=True).returncode == 0
    if has_origin:
        info("Remote 'origin' exists, pushing main")
        return git("push", "-u", "origin", "main", check=False).returncode

    info(f"Creating public GitHub repository {repo}")
    return subprocess.run(create_command, cwd=PROJECT_ROOT).returncode


# ---------------------------------------------------------------------------- main
def main() -> int:
    args = parse_args()
    public_key = resolve_public_key(args)
    system = platform.system()
    info(f"Detected OS: {system} ({platform.machine()}), Python {platform.python_version()}")

    exit_code = 0
    if not args.skip_bootstrap:
        if system == "Windows":
            if not args.skip_venv:
                setup_windows_venv()
            exit_code = run_windows(args, public_key)
        elif system in ("Linux", "Darwin"):
            exit_code = run_unix(args, public_key)
        else:
            fail(f"Unsupported operating system: {system}")

        if exit_code != 0:
            fail(f"Platform bootstrap failed with exit code {exit_code}.", exit_code)

    if args.git:
        exit_code = setup_git(args.repo, push=not args.no_push)

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except subprocess.CalledProcessError as error:
        fail(f"Command failed ({error.returncode}): {' '.join(map(str, error.cmd))}", error.returncode)
