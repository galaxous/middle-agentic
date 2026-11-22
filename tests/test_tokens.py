from __future__ import annotations

import subprocess

import pytest

from middle_agentic import tokens
from middle_agentic.cli import main

PY = "/opt/venv/bin/python"


class FakeSecurity:
    """Stands in for `security`: keeps items in a dict instead of touching the Keychain."""

    def __init__(self) -> None:
        self.items: dict[str, str] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, input=None, capture_output=False, text=False, env=None):
        self.calls.append((list(args), input))
        if args[:2] == ["security", "-i"]:
            line = input.strip()
            service = line.split('-s "')[1].split('"')[0]
            value = line.split('-w "')[1].rsplit('"', 1)[0]
            self.items[service] = value
            return subprocess.CompletedProcess(args, 0, "", "")
        service = args[args.index("-s") + 1]
        if args[1] == "find-generic-password":
            if service in self.items:
                return subprocess.CompletedProcess(args, 0, self.items[service] + "\n", "")
            return subprocess.CompletedProcess(args, 44, "", "not found")
        existed = self.items.pop(service, None) is not None
        return subprocess.CompletedProcess(args, 0 if existed else 44, "", "")


class FakePowerShell:
    """Stands in for powershell: 'encrypts' into a file and decrypts it back. Real DPAPI is not used."""

    def __init__(self) -> None:
        self.scripts: list[str] = []
        self.stdins: list[str | None] = []
        self.argv: list[list[str]] = []

    def __call__(self, args, input=None, capture_output=False, text=False, env=None):
        self.scripts.append(args[-1])
        self.stdins.append(input)
        self.argv.append(list(args))
        path = env["MAGENT_SECRET_FILE"]
        if "ConvertFrom-SecureString" in args[-1]:
            with open(path, "w", encoding="ascii") as handle:
                handle.write("ENC:" + input)
            return subprocess.CompletedProcess(args, 0, "", "")
        with open(path, encoding="ascii") as handle:
            return subprocess.CompletedProcess(args, 0, handle.read().removeprefix("ENC:") + "\n", "")


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    fake = FakeSecurity()
    return tokens.MacKeychain(run=fake, account="tester"), fake


@pytest.fixture
def win(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    fake = FakePowerShell()
    return tokens.WindowsVault(run=fake, directory=tmp_path / "secrets"), fake


# ---- macOS Keychain ---------------------------------------------------------

def test_mac_set_then_get_uses_existing_service_name(mac) -> None:
    store, fake = mac
    tokens.set_token(store, "anthropic", "sk-ant-test-123")
    assert fake.items == {"anthropic-api-key": "sk-ant-test-123"}
    assert tokens.get_token(store, "anthropic") == "sk-ant-test-123"


def test_mac_value_never_appears_in_argv(mac) -> None:
    store, fake = mac
    tokens.set_token(store, "openai", "sk-proj-secret")
    for args, _ in fake.calls:
        assert not any("sk-proj-secret" in part for part in args)


def test_mac_refuses_values_the_interactive_parser_would_mangle(mac) -> None:
    store, fake = mac
    for bad in ('sk "x"', "sk\\x", "sk\nx", ""):
        with pytest.raises(tokens.TokenError):
            tokens.set_token(store, "gemini", bad)
    assert fake.items == {}


def test_missing_token_is_an_error_with_hint(mac) -> None:
    store, _ = mac
    with pytest.raises(tokens.TokenError, match="magent token set openrouter"):
        tokens.get_token(store, "openrouter")


def test_mac_refused_off_darwin(monkeypatch) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "linux")
    with pytest.raises(tokens.TokenError, match="darwin"):
        tokens.MacKeychain(run=FakeSecurity()).get("openai")


# ---- Windows vault ----------------------------------------------------------

def test_win_round_trip_and_value_stays_out_of_argv(win) -> None:
    store, fake = win
    tokens.set_token(store, "openai", "sk-proj-win-secret")
    assert tokens.get_token(store, "openai") == "sk-proj-win-secret"
    for argv in fake.argv:
        assert not any("sk-proj-win-secret" in part for part in argv)
    assert fake.stdins[0] == "sk-proj-win-secret"  # value only goes through stdin


def test_win_delete_and_list_never_print_values(win) -> None:
    store, _ = win
    tokens.set_token(store, "anthropic", "sk-ant-x")
    assert tokens.delete_token(store, "anthropic") == "deleted anthropic token"
    assert tokens.delete_token(store, "anthropic") == "no anthropic token to delete"
    tokens.set_token(store, "anthropic", "sk-ant-x")
    lines = tokens.list_tokens(store)
    assert "anthropic: set (ANTHROPIC_API_KEY)" in lines
    assert "gemini: missing (GEMINI_API_KEY)" in lines
    assert all("sk-ant-x" not in line for line in lines)


def test_win_get_without_file_does_not_call_powershell(win) -> None:
    store, fake = win
    assert store.get("openrouter") is None
    assert fake.scripts == []


def test_win_refused_off_windows(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    with pytest.raises(tokens.TokenError, match="win32"):
        tokens.WindowsVault(run=FakePowerShell(), directory=tmp_path).get("openai")


def test_default_store_follows_platform(monkeypatch) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    assert isinstance(tokens.default_store(), tokens.MacKeychain)
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    assert isinstance(tokens.default_store(), tokens.WindowsVault)
    monkeypatch.setattr(tokens.sys, "platform", "freebsd13")
    with pytest.raises(tokens.TokenError, match="no token store"):
        tokens.default_store()


def test_unknown_provider_is_rejected(mac) -> None:
    store, _ = mac
    with pytest.raises(tokens.TokenError, match="unknown provider"):
        tokens.get_token(store, "mistral")


# ---- shell profiles ---------------------------------------------------------

def test_zsh_lines_use_security_and_hold_no_secret() -> None:
    text = tokens.managed_block("zsh", PY)
    assert 'export OPENAI_API_KEY="$(security find-generic-password -a "$USER" -s openai-api-key -w 2>/dev/null)"' in text
    assert "sk-" not in text and "AQ." not in text


def test_powershell_lines_call_python_module() -> None:
    text = tokens.managed_block("powershell", PY)
    assert f'$env:GEMINI_API_KEY = (& "{PY}" -m middle_agentic token get gemini 2>$null)' in text
    assert "security" not in text


def test_install_shell_adds_missing_exports_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    profile = tmp_path / ".zprofile"
    profile.write_text("source ~/.profile\nexport HISTCONTROL=erasedups\n", encoding="utf-8")

    report = tokens.install_shell(profile, "zsh", python_exe=PY)
    assert any(line.startswith("updated") for line in report)
    assert sum(line.startswith("manage") for line in report) == len(tokens.PROVIDERS)
    assert profile.read_text(encoding="utf-8").startswith("source ~/.profile\nexport HISTCONTROL=erasedups")
    assert (tmp_path / ".zprofile.bak").is_file()

    again = tokens.install_shell(profile, "zsh", python_exe=PY)
    assert again[-1] == "no change"
    assert profile.read_text(encoding="utf-8").count(tokens.BLOCK_START) == 1


def test_install_shell_warns_on_literal_value_without_printing_it(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    profile = tmp_path / ".zprofile"
    profile.write_text('export OPENAI_API_KEY="sk-proj-LITERAL-DO-NOT-PRINT"\n', encoding="utf-8")
    report = tokens.install_shell(profile, "zsh", python_exe=PY)
    joined = "\n".join(report)
    assert "warn   OPENAI_API_KEY" in joined
    assert "LITERAL" not in joined
    assert profile.read_text(encoding="utf-8").count("OPENAI_API_KEY=") == 1
    assert "manage ANTHROPIC_API_KEY" in joined


def test_install_shell_skips_powershell_lookup_outside_block(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    profile = tmp_path / "profile.ps1"
    profile.write_text(f'$env:OPENAI_API_KEY = (& "{PY}" -m middle_agentic token get openai 2>$null)\n', encoding="utf-8")
    assert "skip   OPENAI_API_KEY" in "\n".join(tokens.install_shell(profile, "powershell", python_exe=PY))


def test_install_shell_dry_run_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    profile = tmp_path / ".zprofile"
    profile.write_text("# empty\n", encoding="utf-8")
    report = tokens.install_shell(profile, "zsh", dry_run=True, python_exe=PY)
    assert any(line.startswith("dry run") for line in report)
    assert profile.read_text(encoding="utf-8") == "# empty\n"
    assert not (tmp_path / ".zprofile.bak").exists()


def test_install_shell_creates_missing_profile_and_parent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    profile = tmp_path / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    tokens.install_shell(profile, "powershell", python_exe=PY)
    assert profile.read_text(encoding="utf-8").startswith(tokens.BLOCK_START)


def test_install_shell_refuses_wrong_platform(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    with pytest.raises(tokens.TokenError, match="macOS"):
        tokens.install_shell(tmp_path / "x", "zsh")


def test_default_profile_paths(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.Path, "home", lambda: tmp_path)
    assert tokens.default_profile("zsh") == tmp_path / ".zprofile"
    assert tokens.default_profile("bash") == tmp_path / ".bash_profile"
    assert tokens.default_profile("powershell").name == "Microsoft.PowerShell_profile.ps1"


# ---- CLI --------------------------------------------------------------------

def test_cli_get_prints_bare_value(mac, monkeypatch, capsys) -> None:
    store, _ = mac
    monkeypatch.setattr(tokens, "default_store", lambda: store)
    tokens.set_token(store, "anthropic", "sk-ant-cli")
    capsys.readouterr()
    assert main(["token", "get", "anthropic"]) == 0
    assert capsys.readouterr().out == "sk-ant-cli\n"


def test_cli_get_missing_provider_argument_fails(capsys) -> None:
    assert main(["token", "get"]) != 0
    assert "needs a provider" in capsys.readouterr().err


def test_cli_install_shell_dry_run(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    profile = tmp_path / ".zprofile"
    assert main(["token", "install-shell", "--shell", "zsh", "--profile", str(profile), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not profile.exists()


def test_cli_env_powershell_prints_block(capsys) -> None:
    assert main(["token", "env", "--shell", "powershell"]) == 0
    assert "$env:ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_login_env_plist_holds_lookup_not_secret(monkeypatch) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    plist = tokens.launch_agent_plist()
    assert "launchctl setenv MAGENT_API_KEY" in plist
    assert "-s gateway-api-key" in plist
    assert "mag_" not in plist and "<key>RunAtLoad</key>" in plist


def test_login_env_install_and_remove(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "darwin")
    calls: list[list[str]] = []

    def fake_launchctl(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    plist = tmp_path / "LaunchAgents" / "x.plist"
    tokens.install_login_env(keychain_run=fake_launchctl, plist_path=plist, uid=501)
    assert plist.is_file()
    assert ["launchctl", "bootstrap", "gui/501", str(plist)] in calls
    assert tokens.remove_login_env(keychain_run=fake_launchctl, plist_path=plist, uid=501)[0].startswith("removed")
    assert not plist.exists()


def test_login_env_refused_off_macos(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "win32")
    with pytest.raises(tokens.TokenError, match="macOS"):
        tokens.install_login_env(keychain_run=lambda *a, **k: None, plist_path=tmp_path / "x.plist")


# ---- Linux store ------------------------------------------------------------

class FakeSecretTool:
    def __init__(self, fail_store: bool = False) -> None:
        self.items: dict[str, str] = {}
        self.fail_store = fail_store
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None, capture_output=False, text=False):
        self.calls.append(list(args))
        account = args[args.index("account") + 1]
        if args[1] == "store":
            if self.fail_store:
                return subprocess.CompletedProcess(args, 1, "", "no keyring")
            self.items[account] = input
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1] == "lookup":
            return subprocess.CompletedProcess(args, 0 if account in self.items else 1, self.items.get(account, ""), "")
        existed = self.items.pop(account, None) is not None
        return subprocess.CompletedProcess(args, 0 if existed else 1, "", "")


def test_linux_uses_secret_service_when_a_session_bus_exists(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.shutil, "which", lambda name: "/usr/bin/secret-tool")
    fake = FakeSecretTool()
    store = tokens.LinuxStore(run=fake, directory=tmp_path / "s", session_bus_env=lambda: "unix:path=/run/bus")
    tokens.set_token(store, "gateway", "mag_linux_value")
    assert store.backend() == "secret-service"
    assert tokens.get_token(store, "gateway") == "mag_linux_value"
    assert not (tmp_path / "s").exists()  # nothing written to disk


def test_linux_falls_back_to_a_0600_file_without_a_session_bus(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.shutil, "which", lambda name: "/usr/bin/secret-tool")
    fake = FakeSecretTool()
    store = tokens.LinuxStore(run=fake, directory=tmp_path / "s", session_bus_env=lambda: None)
    tokens.set_token(store, "gateway", "mag_pi_value")
    assert store.backend() == "file"
    assert fake.calls == []  # secret-tool never touched
    file = tmp_path / "s" / "gateway.key"
    assert file.read_text(encoding="utf-8") == "mag_pi_value"
    assert oct(file.stat().st_mode & 0o777) == "0o600"
    assert tokens.get_token(store, "gateway") == "mag_pi_value"
    assert tokens.delete_token(store, "gateway") == "deleted gateway token"
    assert not file.exists()


def test_linux_falls_back_to_file_when_secret_tool_store_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(tokens.shutil, "which", lambda name: "/usr/bin/secret-tool")
    store = tokens.LinuxStore(run=FakeSecretTool(fail_store=True), directory=tmp_path / "s",
                              session_bus_env=lambda: "unix:path=/run/bus")
    tokens.set_token(store, "gateway", "mag_fallback")
    assert (tmp_path / "s" / "gateway.key").read_text(encoding="utf-8") == "mag_fallback"


def test_default_store_on_linux_is_the_linux_store(monkeypatch) -> None:
    monkeypatch.setattr(tokens.sys, "platform", "linux")
    assert isinstance(tokens.default_store(), tokens.LinuxStore)
