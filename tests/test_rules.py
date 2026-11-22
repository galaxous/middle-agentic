from __future__ import annotations

import importlib.util
import json
import tomllib

import pytest

from middle_agentic import integrations, rules
from middle_agentic.cli import main
from middle_agentic.config import PROJECT_ROOT


def test_install_writes_cursor_rule_and_claude_block(tmp_path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# My project\n\nKeep this.\n", encoding="utf-8")

    written = rules.install(tmp_path, ("cursor", "claude"))
    assert written == [tmp_path / ".cursor" / "rules" / "memory.mdc", claude_md]

    cursor_rule = written[0].read_text(encoding="utf-8")
    assert cursor_rule.startswith("---\ndescription:")
    assert "alwaysApply: true" in cursor_rule
    assert f'project_dir: "{tmp_path}"' in cursor_rule

    rules.install(tmp_path, ("cursor", "claude"))  # re-install replaces the block instead of appending a second one
    text = claude_md.read_text(encoding="utf-8")
    assert text.startswith("# My project\n\nKeep this.\n")
    assert text.count(rules.BLOCK_START) == 1 and "memory_record" in text


def test_remove_keeps_user_content(tmp_path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Notes\n", encoding="utf-8")
    rules.install(tmp_path, ("cursor", "claude"))
    assert rules.remove(tmp_path, ("cursor", "claude")) == [tmp_path / rules.CURSOR_RULE, claude_md]
    assert claude_md.read_text(encoding="utf-8") == "# Notes\n"
    assert not (tmp_path / rules.CURSOR_RULE).exists()

    rules.install(tmp_path, ("claude",))
    claude_md.unlink()
    rules.install(tmp_path, ("claude",))
    rules.remove(tmp_path, ("claude",))
    assert not claude_md.exists()


def test_codex_and_antigravity_share_one_agents_md(tmp_path) -> None:
    written = rules.install(tmp_path, ("codex", "antigravity"))
    assert written == [tmp_path / rules.AGENTS_FILE]  # written once, not twice
    text = (tmp_path / rules.AGENTS_FILE).read_text(encoding="utf-8")
    assert text.count(rules.BLOCK_START) == 1

    assert rules.remove(tmp_path, ("codex",)) == [tmp_path / rules.AGENTS_FILE]
    assert not (tmp_path / rules.AGENTS_FILE).exists()


def test_continue_rule_is_owned_file(tmp_path) -> None:
    written = rules.install(tmp_path, ("continue",))
    assert written == [tmp_path / rules.CONTINUE_RULE]
    text = written[0].read_text(encoding="utf-8")
    assert text.startswith("---\nname: middle-agentic memory\n---\n")
    assert f'project_dir: "{tmp_path}"' in text
    assert rules.remove(tmp_path, ("continue",)) == [tmp_path / rules.CONTINUE_RULE]


def test_cli_rules_install(tmp_path, capsys) -> None:
    assert main(["rules", "install", "--tool", "cursor", "--project", str(tmp_path)]) == 0
    assert (tmp_path / rules.CURSOR_RULE).is_file()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "written:" in capsys.readouterr().out


def test_claude_user_install_without_cli_merges_claude_json(tmp_path, monkeypatch) -> None:
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


def test_antigravity_global_install_merges_mcp_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    target = tmp_path / ".gemini" / "config" / "mcp_config.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")

    assert integrations.install_antigravity() == [target, target.with_name("hooks.json")]
    config = json.loads(target.read_text(encoding="utf-8"))
    assert "other" in config["mcpServers"]
    assert config["mcpServers"]["middle-agentic"]["args"] == ["-m", "middle_agentic", "mcp"]


def test_antigravity_project_install(tmp_path) -> None:
    written = integrations.install_antigravity(scope="project", project_dir=tmp_path)
    assert written == [tmp_path / ".agents" / "mcp_config.json", tmp_path / ".agents" / "hooks.json"]
    assert "middle-agentic" in json.loads(written[0].read_text(encoding="utf-8"))["mcpServers"]


def test_antigravity_install_writes_hooks_per_event_with_event_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    hooks_path = tmp_path / ".gemini" / "config" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(json.dumps({"their-hook": {"PostToolUse": []}}), encoding="utf-8")

    integrations.install_antigravity(url="http://100.64.0.2:8000")
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "their-hook" in hooks  # other hook entries are kept
    ours = hooks["middle-agentic"]
    assert set(ours) == set(integrations.ANTIGRAVITY_EVENTS)
    command = ours["PreInvocation"][0]["command"]
    assert " antigravity --event PreInvocation --url http://100.64.0.2:8000" in command


def test_codex_install_writes_hooks_json_with_context_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    integrations.install_codex(url="http://100.64.0.2:8000")
    integrations.install_codex(url="http://100.64.0.2:8000")  # idempotent

    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == set(integrations.CODEX_EVENTS)
    for event in integrations.CODEX_EVENTS:
        assert len(hooks[event]) == 1  # the managed group is replaced, not appended
        handler = hooks[event][0]["hooks"][0]
        assert handler["additionalContextLimit"] == integrations.CODEX_CONTEXT_LIMIT
        assert " codex --url http://100.64.0.2:8000" in handler["command"]


def test_codex_refused_config_leaves_hooks_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[mcp_servers.middle-agentic]\ncommand = "old"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        integrations.install_codex()
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_continue_project_install_is_valid_yaml_shape(tmp_path) -> None:
    yaml = pytest.importorskip("yaml")
    written = integrations.install_continue(scope="project", project_dir=tmp_path)
    assert written == [tmp_path / ".continue" / "mcpServers" / "middle-agentic.yaml"]
    doc = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert doc["schema"] == "v1" and doc["mcpServers"][0]["type"] == "stdio"
    assert doc["mcpServers"][0]["args"] == ["-m", "middle_agentic", "mcp"]


def test_codex_install_writes_valid_toml_and_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "npx"\n', encoding="utf-8")

    integrations.install_codex()
    integrations.install_codex()  # second run replaces the block
    text = config.read_text(encoding="utf-8")
    assert text.count(integrations.CODEX_BLOCK_START) == 1
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5" and "other" in parsed["mcp_servers"]
    assert parsed["mcp_servers"]["middle-agentic"]["args"] == ["-m", "middle_agentic", "mcp"]
    assert config.with_name("config.toml.bak").is_file()


def test_codex_install_refuses_duplicate_hand_written_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(integrations.Path, "home", lambda: tmp_path)
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.middle-agentic]\ncommand = "old"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        integrations.install_codex()
    assert config.read_text(encoding="utf-8").startswith('[mcp_servers.middle-agentic]')


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


def test_integrate_all_runs_every_tool_and_keeps_going_after_a_failure(tmp_path, monkeypatch, capsys) -> None:
    from middle_agentic import cli

    calls: list[str] = []

    def recorder(name, fail=False):
        def install(**_kwargs):
            calls.append(name)
            if fail:
                raise RuntimeError("boom")
            return [tmp_path / f"{name}.json"]
        return install

    for name in ("install_cursor", "install_claude", "install_codex", "install_antigravity", "install_continue"):
        monkeypatch.setattr(integrations, name, recorder(name.removeprefix("install_"), fail=name == "install_codex"))
    monkeypatch.setattr(cli, "_default_hook_url", lambda: None)

    assert main(["integrate", "all", "--scope", "user"]) == 1  # codex failed
    assert calls == ["cursor", "claude", "codex", "antigravity", "continue"]
    assert "failed: codex" in capsys.readouterr().err
