from __future__ import annotations

import importlib.util

import pytest

from middle_agentic import rules
from middle_agentic.cli import main
from middle_agentic.config import PROJECT_ROOT


def test_install_writes_cursor_rule_and_claude_block(tmp_path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My project\n\nKeep this.\n", encoding="utf-8")

    written = rules.install(tmp_path)
    assert written == [tmp_path / ".cursor" / "rules" / "memory.mdc", claude_md]

    cursor_rule = written[0].read_text(encoding="utf-8")
    assert cursor_rule.startswith("---\ndescription:")
    assert "alwaysApply: true" in cursor_rule
    assert f'project_dir: "{tmp_path}"' in cursor_rule

    rules.install(tmp_path)  # re-install replaces the block instead of appending a second one
    text = claude_md.read_text(encoding="utf-8")
    assert text.startswith("# My project\n\nKeep this.\n")
    assert text.count(rules.BLOCK_START) == 1 and "memory_record" in text


def test_remove_keeps_user_content(tmp_path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Notes\n", encoding="utf-8")
    rules.install(tmp_path)
    assert rules.remove(tmp_path) == [tmp_path / rules.CURSOR_RULE, claude_md]
    assert claude_md.read_text(encoding="utf-8") == "# Notes\n"
    assert not (tmp_path / rules.CURSOR_RULE).exists()

    rules.install(tmp_path, ("claude",))
    claude_md.unlink()
    rules.install(tmp_path, ("claude",))
    rules.remove(tmp_path, ("claude",))
    assert not claude_md.exists()


def test_cli_rules_install(tmp_path, capsys) -> None:
    assert main(["rules", "install", "--tool", "cursor", "--project", str(tmp_path)]) == 0
    assert (tmp_path / rules.CURSOR_RULE).is_file()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "written:" in capsys.readouterr().out


def test_claude_user_install_without_cli_merges_claude_json(tmp_path, monkeypatch) -> None:
    import json

    from middle_agentic import integrations

    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(integrations.shutil, "which", lambda _: None)
    (tmp_path / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": {"other": {}}}), encoding="utf-8")

    written = integrations.install_claude(url="http://100.64.0.2:8000")
    assert written == [tmp_path / ".claude" / "settings.json", tmp_path / ".claude.json"]

    claude_json = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert claude_json["numStartups"] == 3 and "other" in claude_json["mcpServers"]
    assert claude_json["mcpServers"]["middle-agentic"]["args"] == ["-m", "middle_agentic", "mcp"]
    settings = json.loads(written[0].read_text(encoding="utf-8"))
    assert "--url http://100.64.0.2:8000" in settings["hooks"]["Stop"][0]["hooks"][0]["command"]


@pytest.fixture
def hook_module():
    spec = importlib.util.spec_from_file_location("magent_hook", PROJECT_ROOT / "integrations" / "hooks" / "magent_hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_hook_url_resolution_order(hook_module, monkeypatch) -> None:
    for name in ("MAGENT_URL", "MAGENT_DEFAULT_NODE", "MAGENT_PORT", "GATEWAY_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert hook_module.resolve_base_url() == "http://127.0.0.1:8000"

    monkeypatch.setenv("MAGENT_DEFAULT_NODE", "office-mac")
    assert hook_module.resolve_base_url() == "http://office-mac:8000"
    monkeypatch.setenv("MAGENT_DEFAULT_NODE", "http://100.64.0.2:9000")
    assert hook_module.resolve_base_url() == "http://100.64.0.2:9000"

    monkeypatch.setenv("MAGENT_URL", "http://gateway:8000")
    assert hook_module.resolve_base_url() == "http://gateway:8000"
    assert hook_module.resolve_base_url("http://explicit:1") == "http://explicit:1"
