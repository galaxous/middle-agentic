"""Moving a checkout-based gateway onto the installed package: the checkout is only read."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from middle_agentic import cli, gateway_setup, service


def _make_checkout(repo: Path) -> None:
    repo.mkdir(parents=True)
    connection = sqlite3.connect(repo / "agent_memory.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, content TEXT)")
    connection.executemany("INSERT INTO nodes VALUES (?, ?)", [(str(i), f"memory {i}") for i in range(50)])
    connection.commit()  # stays in the -wal file until checkpoint: the backup must still include it
    connection.close()   # closing checkpoints the WAL now, so the fingerprint below is stable
    (repo / ".env").write_text(
        "GEMINI_API_KEY=placeholder\nMEMORY_DB_PATH=/old/repo/agent_memory.db\nMAGENT_URL=http://100.68.92.41:8000\n",
        encoding="utf-8",
    )
    (repo / "config.yaml").write_text("node: {name: gw, roles: [gateway, llm]}\n", encoding="utf-8")


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def test_migration_copies_everything_and_never_touches_the_checkout(tmp_path) -> None:
    repo, home = tmp_path / "checkout", tmp_path / "home"
    _make_checkout(repo)
    before = _fingerprint(repo)
    messages = gateway_setup.migrate(repo, home=home, stop_service=lambda: "stopped")

    assert _fingerprint(repo) == before  # read only
    copied = sqlite3.connect(home / "agent_memory.db")
    assert copied.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 50  # WAL content included
    copied.close()

    env = home / ".env"
    text = env.read_text(encoding="utf-8")
    assert "MEMORY_DB_PATH" not in text and "GEMINI_API_KEY=placeholder" in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert (home / "config.yaml").is_file()
    assert messages[0] == "stopped"


def test_existing_database_is_not_overwritten_without_force(tmp_path) -> None:
    repo, home = tmp_path / "checkout", tmp_path / "home"
    _make_checkout(repo)
    home.mkdir()
    (home / "agent_memory.db").write_bytes(b"not replaced")
    with pytest.raises(gateway_setup.GatewaySetupError, match="already exists"):
        gateway_setup.migrate(repo, home=home, stop_service=lambda: "stopped")
    assert (home / "agent_memory.db").read_bytes() == b"not replaced"


def test_no_checkout_means_only_the_service_is_stopped(tmp_path) -> None:
    messages = gateway_setup.migrate(None, home=tmp_path / "home", stop_service=lambda: "stopped")
    assert messages[0] == "stopped" and "no checkout" in messages[1]


def test_health_wait_succeeds_and_times_out(monkeypatch) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return self.body

    monkeypatch.setattr(gateway_setup.urllib.request, "urlopen", lambda *a, **k: Response(b'{"status": "ok", "service": "middle-agentic"}'))
    assert "healthy" in gateway_setup.wait_for_health(8000, timeout=2)

    def refuse(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(gateway_setup.urllib.request, "urlopen", refuse)
    with pytest.raises(gateway_setup.GatewaySetupError, match="did not become healthy"):
        gateway_setup.wait_for_health(8000, timeout=1)


def test_cli_stops_the_old_service_before_copying_and_starts_the_installed_one(monkeypatch, tmp_path, capsys) -> None:
    repo, home = tmp_path / "checkout", tmp_path / "home"
    _make_checkout(repo)
    calls: list[str] = []
    monkeypatch.setattr(gateway_setup, "magent_home", lambda: home)
    monkeypatch.setattr(service, "uninstall", lambda: calls.append("uninstall") or "stopped\nsecond line")
    monkeypatch.setattr(service, "install", lambda: calls.append("install") or "started")
    monkeypatch.setattr(gateway_setup, "wait_for_health", lambda port, timeout=30: calls.append(f"health:{port}") or "healthy")
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: type("S", (), {"gateway": type("G", (), {"port": 8000})()})())

    original_backup = gateway_setup._backup_database

    def recording_backup(source, target, force):
        calls.append("copy")
        return original_backup(source, target, force)

    monkeypatch.setattr(gateway_setup, "_backup_database", recording_backup)

    assert cli.main(["gateway-setup", "--from-repo", str(repo)]) == 0
    assert calls == ["uninstall", "copy", "install", "health:8000"]
    assert (home / "agent_memory.db").is_file()
