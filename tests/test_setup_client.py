"""Client setup wizard: verify first, write only after confirmation."""
from __future__ import annotations

import httpx
import pytest

from middle_agentic import setup_client


def gateway(health=200, mcp=200, memory=200, health_body=None):
    health_body = health_body or {"service": "middle-agentic", "version": "0.3.0", "node": {"name": "gw"}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(health, json=health_body)
        if request.url.path == "/mcp/":
            return httpx.Response(mcp, json={"jsonrpc": "2.0", "id": 1, "result": {}})
        if request.url.path == "/v1/memory/context":
            return httpx.Response(memory, json={"context": ""})
        raise AssertionError(request.url)

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    def set(self, provider: str, value: str) -> None:
        self.saved[provider] = value

    def backend(self) -> str:
        return "file"


def run(client, *, answers=("e",), key="mag_test", url="100.68.92.41"):
    store = FakeStore()
    integrated: list[str] = []
    logins: list[bool] = []
    asked = iter(answers)
    lines: list[str] = []
    code = setup_client.run(
        url, key, assume_yes=False, ask=lambda _q: next(asked), secret=lambda _q: "unused",
        client=client, store=store, integrate=lambda u: integrated.append(u) or 0,
        login_env=lambda: logins.append(True), out=lines.append,
    )
    return code, store, integrated, logins, "\n".join(lines)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGENT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def test_normalise_url() -> None:
    assert setup_client.normalise_url("100.68.92.41") == "http://100.68.92.41:8000"
    assert setup_client.normalise_url("https://gw.example.com/") == "https://gw.example.com:8000"
    assert setup_client.normalise_url("http://host:9000") == "http://host:9000"
    with pytest.raises(ValueError):
        setup_client.normalise_url("ftp://host")
    with pytest.raises(ValueError):
        setup_client.normalise_url("   ")


def test_success_stores_key_writes_url_and_integrates(home) -> None:
    code, store, integrated, logins, out = run(gateway())
    assert code == 0
    assert store.saved == {"gateway": "mag_test"}
    assert integrated == ["http://100.68.92.41:8000"]
    assert logins == [True]
    assert (home / ".env").read_text(encoding="utf-8") == "MAGENT_URL=http://100.68.92.41:8000\n"
    assert "✓ key accepted by the gateway" in out and "✓ gateway reachable" in out


def test_declining_the_final_question_writes_nothing(home) -> None:
    code, store, integrated, logins, out = run(gateway(), answers=("h",))
    assert code == 0 and store.saved == {} and integrated == [] and logins == []
    assert not (home / ".env").exists()
    assert "iptal" in out


def test_unreachable_or_not_a_gateway_changes_nothing(home) -> None:
    code, store, integrated, _, out = run(gateway(health=200, health_body={"service": "something-else"}))
    assert code == 1 and store.saved == {} and integrated == []
    assert not (home / ".env").exists()
    assert "hiçbir şey değişmedi" in out


def test_rejected_key_changes_nothing(home) -> None:
    code, store, integrated, _, out = run(gateway(mcp=401))
    assert code == 1 and store.saved == {} and integrated == []
    assert "rejected" in out or "reddetti" in out or "✗ key accepted" in out
    assert not (home / ".env").exists()


def test_missing_memory_read_scope_is_reported_and_blocks_setup(home) -> None:
    code, store, _, _, out = run(gateway(memory=403))
    assert code == 1 and store.saved == {}
    assert "memory:read" in out


def test_set_env_value_keeps_other_lines(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("# my notes\nMAGENT_URL=http://old:8000\nOTHER=1\n", encoding="utf-8")
    setup_client.set_env_value(env, "MAGENT_URL", "http://new:8000")
    assert env.read_text(encoding="utf-8") == "# my notes\nMAGENT_URL=http://new:8000\nOTHER=1\n"
