"""Project rules that teach each agent tool to use the middle-agentic memory tools.

cursor:      <project>/.cursor/rules/memory.mdc (always-applied rule, file owned by middle-agentic)
claude:      <project>/CLAUDE.md, inside a marked block so the rest of the file is left alone
codex:       <project>/AGENTS.md, marked block (Codex reads AGENTS.md)
antigravity: <project>/AGENTS.md, same block as codex (Antigravity reads AGENTS.md too)
continue:    <project>/.continue/rules/middle-agentic-memory.md (file owned by middle-agentic)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

CURSOR_RULE = Path(".cursor") / "rules" / "memory.mdc"
CLAUDE_FILE = Path("CLAUDE.md")
AGENTS_FILE = Path("AGENTS.md")
CONTINUE_RULE = Path(".continue") / "rules" / "middle-agentic-memory.md"
BLOCK_START = "<!-- middle-agentic:memory:start -->"
BLOCK_END = "<!-- middle-agentic:memory:end -->"
TOOLS = ("cursor", "claude", "codex", "antigravity", "continue")
# Tools whose instructions live in a marked block inside a shared markdown file.
_BLOCK_FILES = {"claude": CLAUDE_FILE, "codex": AGENTS_FILE, "antigravity": AGENTS_FILE}

_BLOCK_PATTERN = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)


def memory_instructions(project_dir: str) -> str:
    return f"""# Project memory (middle-agentic)

This project has a persistent memory shared by all agent tools used here (Cursor, Claude Code, Codex,
Antigravity, Continue), served by the `middle-agentic` MCP server (`memory_search`, `memory_record`).
Always pass `project_dir: "{project_dir}"` to these tools so they read and write this project's memory.

## Before working
- At the start of a non-trivial task, or whenever the user refers to earlier work ("as we decided",
  "last time", "why did we..."), call `memory_search` with a few specific keywords: component names,
  technologies, error messages. Search again with different words if the first try finds nothing.
- Treat results as hints that may be outdated. Verify them against the code before relying on them.
- Results tagged `[decision]` or `[fact]` are curated and take precedence over raw conversation.

## After working
When something worth remembering happens, call `memory_record` with one short, self-contained sentence
that makes sense without this conversation:
- `role: "decision"`: an architectural or product choice and its reason.
  Example: "Auth uses JWT in httpOnly cookies because the mobile app shares the API."
- `role: "fact"`: a stable project fact (setup, conventions, environments, commands).
  Example: "Tests run with `pytest -q`; integration tests need the gateway on port 8000."
- `role: "note"`: anything else useful later (root cause of a bug, a gotcha, an open TODO).

## Guidelines
- One fact per record. Prefer a few precise records over long summaries.
- When a decision replaces an earlier one, record the new decision and say what it supersedes.
- Never record secrets, API keys, tokens, passwords or personal data.
- Don't record what the code or git history already shows plainly.
"""


def cursor_rule(project_dir: str) -> str:
    frontmatter = (
        "---\n"
        "description: Use the middle-agentic project memory (memory_search / memory_record)\n"
        "alwaysApply: true\n"
        "---\n\n"
    )
    return frontmatter + memory_instructions(project_dir)


def continue_rule(project_dir: str) -> str:
    # Continue rules take an optional frontmatter block; `name` is the rule's display name.
    frontmatter = "---\nname: middle-agentic memory\n---\n\n"
    return frontmatter + memory_instructions(project_dir)


def claude_block(project_dir: str) -> str:
    return f"{BLOCK_START}\n{memory_instructions(project_dir)}{BLOCK_END}\n"


def _project_path(project_dir: Path) -> str:
    # abspath, not resolve: the session id must match the path the editor reports for the workspace.
    return os.path.abspath(project_dir)


def _write_block(path: Path, block: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _BLOCK_PATTERN.search(existing):
        updated = _BLOCK_PATTERN.sub(lambda _: block, existing, count=1)
    else:
        updated = f"{existing.rstrip()}\n\n{block}" if existing.strip() else block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")


def _remove_block(path: Path) -> bool:
    if not path.is_file():
        return False
    existing = path.read_text(encoding="utf-8")
    updated = _BLOCK_PATTERN.sub("", existing).strip()
    if updated == existing.strip():
        return False
    if updated:
        path.write_text(updated + "\n", encoding="utf-8")
    else:
        path.unlink()
    return True


def install(project_dir: Path, tools: tuple[str, ...] = TOOLS) -> list[Path]:
    root = Path(_project_path(project_dir))
    written: list[Path] = []
    if "cursor" in tools:
        path = root / CURSOR_RULE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cursor_rule(str(root)), encoding="utf-8")
        written.append(path)
    if "continue" in tools:
        path = root / CONTINUE_RULE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(continue_rule(str(root)), encoding="utf-8")
        written.append(path)
    # codex and antigravity share AGENTS.md: write it once even if both are requested.
    for tool, relative in _BLOCK_FILES.items():
        if tool not in tools:
            continue
        path = root / relative
        if path in written:
            continue
        _write_block(path, claude_block(str(root)))
        written.append(path)
    return written


def remove(project_dir: Path, tools: tuple[str, ...] = TOOLS) -> list[Path]:
    root = Path(_project_path(project_dir))
    removed: list[Path] = []
    if "cursor" in tools:
        path = root / CURSOR_RULE
        if path.is_file():
            path.unlink()
            removed.append(path)
    if "continue" in tools:
        path = root / CONTINUE_RULE
        if path.is_file():
            path.unlink()
            removed.append(path)
    for tool, relative in _BLOCK_FILES.items():
        path = root / relative
        if tool in tools and path not in removed and _remove_block(path):
            removed.append(path)
    return removed
