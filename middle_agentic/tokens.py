"""API tokens per user, managed with `magent token`.

Storage, per platform:
  macOS    login Keychain via `security`. Service `<provider>-api-key`, account `$USER` (same items
           that ~/.zprofile and ~/.claude/settings.json already look up).
  Windows  one file per provider in %LOCALAPPDATA%\\middle-agentic\\secrets, encrypted by PowerShell
           `ConvertFrom-SecureString` (DPAPI, current user only). Decryptable only by this Windows user.

Values reach `security` / `powershell` through stdin, never through argv, so they do not show in `ps`.

Shell profiles (`install-shell`) get lookups, never values:
  zsh   ~/.zprofile          export VAR="$(security find-generic-password ...)"   (macOS)
  bash  ~/.bash_profile      same lines                                            (macOS)
  powershell  $PROFILE       $env:VAR = (& "<python>" -m middle_agentic token get <p>)  (Windows)
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# provider -> environment variable the provider's SDK/CLI reads
PROVIDERS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",  # Claude Code, Anthropic SDK
    "openai": "OPENAI_API_KEY",        # Codex CLI, OpenAI SDK
    "gemini": "GEMINI_API_KEY",        # Gemini CLI, Antigravity, Continue
    "openrouter": "OPENROUTER_API_KEY",
}
SHELLS = ("zsh", "bash", "powershell")
BLOCK_START = "# >>> middle-agentic tokens (managed by magent token) >>>"
BLOCK_END = "# <<< middle-agentic tokens (managed by magent token) <<<"

# `export VAR=...` (zsh/bash) or `$env:VAR = ...` (PowerShell)
_EXPORT_LINE = re.compile(r"^\s*(?:export\s+|\$env:)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_MACOS_FORBIDDEN = set(" \t\r\n\"'\\")


class TokenError(RuntimeError):
    pass


def _check_provider(name: str) -> str:
    if name not in PROVIDERS:
        raise TokenError(f"unknown provider '{name}', choose one of: {', '.join(PROVIDERS)}")
    return name


@dataclass
class MacKeychain:
    """Login Keychain through the `security` CLI."""

    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    account: str = field(default_factory=lambda: os.environ.get("USER", ""))

    @staticmethod
    def service(provider: str) -> str:
        return f"{provider}-api-key"

    def _check(self) -> None:
        if sys.platform != "darwin":
            raise TokenError("the macOS Keychain backend runs on darwin only")

    def get(self, provider: str) -> str | None:
        self._check()
        result = self.run(
            ["security", "find-generic-password", "-a", self.account, "-s", self.service(provider), "-w"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n") or None

    def set(self, provider: str, value: str) -> None:
        self._check()
        if not value or _MACOS_FORBIDDEN & set(value):
            raise TokenError("token is empty or contains whitespace, quotes or backslashes")
        # `security -i` reads the command from stdin, so the value is not in argv.
        command = f'add-generic-password -U -a "{self.account}" -s "{self.service(provider)}" -w "{value}"\n'
        result = self.run(["security", "-i"], input=command, capture_output=True, text=True)
        if result.returncode != 0:
            raise TokenError(f"keychain write failed: {result.stderr.strip() or 'unknown error'}")

    def delete(self, provider: str) -> bool:
        self._check()
        result = self.run(
            ["security", "delete-generic-password", "-a", self.account, "-s", self.service(provider)],
            capture_output=True, text=True,
        )
        return result.returncode == 0


_PS_SET = (
    "$s = [Console]::In.ReadToEnd(); "
    "$s | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString "
    "| Set-Content -LiteralPath $env:MAGENT_SECRET_FILE -Encoding ascii"
)
_PS_GET = (
    "$enc = Get-Content -LiteralPath $env:MAGENT_SECRET_FILE -Raw; "
    "$ss = $enc | ConvertTo-SecureString; "
    "[Runtime.InteropServices.Marshal]::PtrToStringBSTR("
    "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss))"
)


def default_secrets_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "middle-agentic" / "secrets"


@dataclass
class WindowsVault:
    """Per-user DPAPI-encrypted files, read and written through PowerShell."""

    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    directory: Path = field(default_factory=default_secrets_dir)

    def _check(self) -> None:
        if sys.platform != "win32":
            raise TokenError("the Windows vault backend runs on win32 only")

    def _file(self, provider: str) -> Path:
        return self.directory / f"{provider}.dpapi"

    def _powershell(self, script: str, file: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "MAGENT_SECRET_FILE": str(file)}
        return self.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=stdin, capture_output=True, text=True, env=env,
        )

    def get(self, provider: str) -> str | None:
        self._check()
        file = self._file(provider)
        if not file.is_file():
            return None
        result = self._powershell(_PS_GET, file)
        if result.returncode != 0:
            raise TokenError(f"could not decrypt {provider} token: {result.stderr.strip() or 'unknown error'}")
        return result.stdout.rstrip("\r\n") or None

    def set(self, provider: str, value: str) -> None:
        self._check()
        if not value:
            raise TokenError("token is empty")
        self.directory.mkdir(parents=True, exist_ok=True)
        file = self._file(provider)
        result = self._powershell(_PS_SET, file, stdin=value)
        if result.returncode != 0:
            raise TokenError(f"could not encrypt {provider} token: {result.stderr.strip() or 'unknown error'}")

    def delete(self, provider: str) -> bool:
        self._check()
        file = self._file(provider)
        if not file.is_file():
            return False
        file.unlink()
        return True


def default_store() -> MacKeychain | WindowsVault:
    if sys.platform == "darwin":
        return MacKeychain()
    if sys.platform == "win32":
        return WindowsVault()
    raise TokenError(f"no token store for platform '{sys.platform}' (supported: macOS, Windows)")


def set_token(store, provider: str, value: str | None = None) -> str:
    _check_provider(provider)
    secret = value if value is not None else getpass.getpass(f"{PROVIDERS[provider]} value: ")
    store.set(provider, secret.strip())
    return f"stored {provider} token"


def get_token(store, provider: str) -> str:
    _check_provider(provider)
    value = store.get(provider)
    if value is None:
        raise TokenError(f"no {provider} token stored; run: magent token set {provider}")
    return value


def delete_token(store, provider: str) -> str:
    _check_provider(provider)
    return f"deleted {provider} token" if store.delete(provider) else f"no {provider} token to delete"


def list_tokens(store) -> list[str]:
    """Provider names with a stored token. Values are never printed."""
    return [f"{name}: {'set' if store.get(name) else 'missing'} ({PROVIDERS[name]})" for name in PROVIDERS]


def _lookup_line(shell: str, provider: str, variable: str, python_exe: str) -> str:
    if shell == "powershell":
        # Runs the same vault code; the value goes to the env var, never to the profile file.
        return f'$env:{variable} = (& "{python_exe}" -m middle_agentic token get {provider} 2>$null)'
    service = MacKeychain.service(provider)
    return f'export {variable}="$(security find-generic-password -a "$USER" -s {service} -w 2>/dev/null)"'


def managed_block(shell: str, python_exe: str = sys.executable, providers: list[str] | None = None) -> str:
    names = providers if providers is not None else list(PROVIDERS)
    body = [_lookup_line(shell, p, PROVIDERS[p], python_exe) for p in names]
    return "\n".join([BLOCK_START, *body, BLOCK_END])


def shell_exports(shell: str = "zsh") -> str:
    return managed_block(shell)


def default_profile(shell: str) -> Path:
    if shell == "zsh":
        return Path.home() / ".zprofile"
    if shell == "bash":
        return Path.home() / ".bash_profile"
    if shell == "powershell":
        return Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    raise TokenError(f"unknown shell '{shell}', choose one of: {', '.join(SHELLS)}")


def install_shell(path: Path, shell: str, dry_run: bool = False, python_exe: str = sys.executable) -> list[str]:
    """Keep the managed block of token lookups in a shell profile up to date.

    A provider exported outside the managed block is left alone. A literal value there is
    reported as `warn` without being printed. The block is regenerated, so re-running changes
    nothing. A backup is written before the file changes.
    """
    if shell not in SHELLS:
        raise TokenError(f"unknown shell '{shell}'")
    if shell != "powershell" and sys.platform != "darwin":
        raise TokenError(f"{shell} lookups use the macOS Keychain; run this on macOS or use --shell powershell")
    if shell == "powershell" and sys.platform != "win32":
        raise TokenError("powershell lookups use the Windows vault; run this on Windows")

    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    outside = re.sub(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", "", text, flags=re.DOTALL)

    exported: dict[str, str] = {}
    for line in outside.splitlines():
        match = _EXPORT_LINE.match(line)
        if match:
            exported[match.group(1)] = match.group(2)

    report: list[str] = []
    managed: list[str] = []
    for provider, variable in PROVIDERS.items():
        if variable in exported:
            value = exported[variable]
            if "security " in value or "middle_agentic token get" in value:
                report.append(f"skip   {variable} (already a lookup outside the block)")
            else:
                report.append(f"warn   {variable} is a literal value outside the block; remove that line")
            continue
        managed.append(provider)
        report.append(f"manage {variable}")

    base = outside.rstrip()
    block = managed_block(shell, python_exe, managed) if managed else ""
    if block:
        new_text = f"{base}\n\n{block}\n" if base else f"{block}\n"
    else:
        new_text = f"{base}\n" if base else ""

    if new_text == text:
        return report + ["no change"]
    if dry_run:
        return report + [f"dry run: would update {path}"]

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text(new_text, encoding="utf-8")
    return report + [f"updated {path} (backup: {path.name}.bak)"]
