"""Project rules that teach Cursor and Claude Code to use the middle-agentic memory tools.

Cursor:      <project>/.cursor/rules/memory.mdc (always-applied rule, file owned by middle-agentic)
Claude Code: <project>/CLAUDE.md, inside a marked block so the rest of the file is left alone
"""
from __future__ import annotations

import os
import re
from pathlib import Path

CURSOR_RULE = Path(".cursor") / "rules" / "memory.mdc"
CLAUDE_FILE = Path("CLAUDE.md")
BLOCK_START = "<!-- middle-agentic:memory:start -->"
BLOCK_END = "<!-- middle-agentic:memory:end -->"
TOOLS = ("cursor", "claude")

_BLOCK_PATTERN = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?", re.DOTALL)


def memory_instructions(project_dir: str) -> str:
    return f"""# Project memory (middle-agentic)

This project has a persistent memory shared by Cursor and Claude Code, served by the `middle-agentic`
MCP server (`memory_search`, `memory_record`). Always pass `project_dir: "{project_dir}"` to these
tools so they read and write this project's memory.

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


def claude_block(project_dir: str) -> str:
    return f"{BLOCK_START}\n{memory_instructions(project_dir)}{BLOCK_END}\n"


def _project_path(project_dir: Path) -> str:
    # abspath, not resolve: the session id must match the path the editor reports for the workspace.
    return os.path.abspath(project_dir)


def install(project_dir: Path, tools: tuple[str, ...] = TOOLS) -> list[Path]:
    root = Path(_project_path(project_dir))
    written: list[Path] = []
    if "cursor" in tools:
        path = root / CURSOR_RULE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cursor_rule(str(root)), encoding="utf-8")
        written.append(path)
    if "claude" in tools:
        path = root / CLAUDE_FILE
        block = claude_block(str(root))
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if _BLOCK_PATTERN.search(existing):
            updated = _BLOCK_PATTERN.sub(lambda _: block, existing, count=1)
        else:
            updated = f"{existing.rstrip()}\n\n{block}" if existing.strip() else block
        path.write_text(updated, encoding="utf-8")
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
    if "claude" in tools:
        path = root / CLAUDE_FILE
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            updated = _BLOCK_PATTERN.sub("", existing).strip()
            if updated != existing.strip():
                if updated:
                    path.write_text(updated + "\n", encoding="utf-8")
                else:
                    path.unlink()
                removed.append(path)
    return removed
